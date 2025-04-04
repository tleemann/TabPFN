"""TabPFNClassifier class.

!!! example
    ```python
    import sklearn.datasets
    from tabpfn import TabPFNClassifier

    model = TabPFNClassifier()

    X, y = sklearn.datasets.load_iris(return_X_y=True)

    model.fit(X, y)
    predictions = model.predict(X)
    ```
"""

#  Copyright (c) Prior Labs GmbH 2025.

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, List
from typing_extensions import Self

import numpy as np
import torch
from sklearn.base import BaseEstimator, ClassifierMixin, check_is_fitted
from sklearn.preprocessing import LabelEncoder
from sklearn.exceptions import NotFittedError
from torch.utils.data import Dataset

from tabpfn.base import (
    create_inference_engine,
    determine_precision,
    initialize_tabpfn_model,
)
from tabpfn.model.encoders import SequentialEncoder
from tabpfn.config import ModelInterfaceConfig
from tabpfn.constants import (
    PROBABILITY_EPSILON_ROUND_ZERO,
    SKLEARN_16_DECIMAL_PRECISION,
    XType,
    YType,
)
from tabpfn.preprocessing import (
    ClassifierEnsembleConfig,
    EnsembleConfig,
    default_classifier_preprocessor_configs,
    PreprocessorConfig,
    DatasetCollectionWithPreprocessing
)
from tabpfn.utils import (
    _fix_dtypes,
    _get_embeddings,
    _get_ordinal_encoder,
    infer_categorical_features,
    infer_device_and_type,
    infer_random_state,
    update_encoder_params,
    validate_X_predict,
    validate_Xy_fit,
    split_large_data
)

from tabpfn.inference import InferenceEngineBatchedNoPreprocessing

if TYPE_CHECKING:
    import numpy.typing as npt
    from sklearn.compose import ColumnTransformer
    from torch.types import _dtype

    from tabpfn.inference import (
        InferenceEngine,
    )
    from tabpfn.model.config import InferenceConfig

    try:
        from sklearn.base import Tags
    except ImportError:
        Tags = Any


class TabPFNClassifier(ClassifierMixin, BaseEstimator):
    """TabPFNClassifier class."""

    config_: InferenceConfig
    """The configuration of the loaded model to be used for inference."""

    interface_config_: ModelInterfaceConfig
    """Additional configuration of the interface for expert users."""

    device_: torch.device
    """The device determined to be used."""

    feature_names_in_: npt.NDArray[Any]
    """The feature names of the input data.

    May not be set if the input data does not have feature names,
    such as with a numpy array.
    """

    n_features_in_: int
    """The number of features in the input data used during `fit()`."""

    inferred_categorical_indices_: list[int]
    """The indices of the columns that were inferred to be categorical,
    as a product of any features deemed categorical by the user and what would
    work best for the model.
    """

    classes_: npt.NDArray[Any]
    """The unique classes found in the target data during `fit()`."""

    n_classes_: int
    """The number of classes found in the target data during `fit()`."""

    class_counts_: npt.NDArray[Any]
    """The number of classes per class found in the target data during `fit()`."""

    n_outputs_: Literal[1]
    """The number of outputs the model has. Only 1 for now"""

    use_autocast_: bool
    """Whether torch's autocast should be used."""

    forced_inference_dtype_: _dtype | None
    """The forced inference dtype for the model based on `inference_precision`."""

    executor_: InferenceEngine
    """The inference engine used to make predictions."""

    label_encoder_: LabelEncoder
    """The label encoder used to encode the target variable."""

    preprocessor_: ColumnTransformer
    """The column transformer used to preprocess the input data to be numeric."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        n_estimators: int = 4,
        categorical_features_indices: Sequence[int] | None = None,
        softmax_temperature: float = 0.9,
        balance_probabilities: bool = False,
        average_before_softmax: bool = False,
        model_path: str | Path | Literal["auto"] = "auto",
        device: str | torch.device | Literal["auto"] = "auto",
        ignore_pretraining_limits: bool = False,
        inference_precision: _dtype | Literal["autocast", "auto"] = "auto",
        fit_mode: Literal[
            "low_memory",
            "fit_preprocessors",
            "fit_with_cache",
        ] = "fit_preprocessors",
        memory_saving_mode: bool | Literal["auto"] | float | int = "auto",
        random_state: int | np.random.RandomState | np.random.Generator | None = 0,
        n_jobs: int = -1,
        inference_config: dict | ModelInterfaceConfig | None = None,
        differentiable_input: bool = False,
    ) -> None:
        """A TabPFN interface for classification.

        Args:
            n_estimators:
                The number of estimators in the TabPFN ensemble. We aggregate the
                 predictions of `n_estimators`-many forward passes of TabPFN. Each
                 forward pass has (slightly) different input data. Think of this as an
                 ensemble of `n_estimators`-many "prompts" of the input data.

            categorical_features_indices:
                The indices of the columns that are suggested to be treated as
                categorical. If `None`, the model will infer the categorical columns.
                If provided, we might ignore some of the suggestion to better fit the
                data seen during pre-training.

                !!! note
                    The indices are 0-based and should represent the data passed to
                    `.fit()`. If the data changes between the initializations of the
                    model and the `.fit()`, consider setting the
                    `.categorical_features_indices` attribute after the model was
                    initialized and before `.fit()`.

            softmax_temperature:
                The temperature for the softmax function. This is used to control the
                confidence of the model's predictions. Lower values make the model's
                predictions more confident. This is only applied when predicting during
                a post-processing step. Set `softmax_temperature=1.0` for no effect.

            balance_probabilities:
                Whether to balance the probabilities based on the class distribution
                in the training data. This can help to improve predictive performance
                when the classes are highly imbalanced and the metric of interest is
                insensitive to class imbalance (e.g., balanced accuracy, balanced log
                loss, roc-auc macro ovo, etc.). This is only applied when predicting
                during a post-processing step.

            average_before_softmax:
                Only used if `n_estimators > 1`. Whether to average the predictions of
                the estimators before applying the softmax function. This can help to
                improve predictive performance when there are many classes or when
                calibrating the model's confidence. This is only applied when predicting
                during a post-processing.

                - If `True`, the predictions are averaged before applying the softmax
                  function. Thus, we average the logits of TabPFN and then apply the
                  softmax.
                - If `False`, the softmax function is applied to each set of logits.
                  Then, we average the resulting probabilities of each forward pass.

            model_path:
                The path to the TabPFN model file, i.e., the pre-trained weights.

                - If `"auto"`, the model will be downloaded upon first use. This
                  defaults to your system cache directory, but can be overwritten
                  with the use of an environment variable `TABPFN_MODEL_CACHE_DIR`.
                - If a path or a string of a path, the model will be loaded from
                  the user-specified location if available, otherwise it will be
                  downloaded to this location.

            device:
                The device to use for inference with TabPFN. If `"auto"`, the device
                is `"cuda"` if available, otherwise `"cpu"`.

                See PyTorch's documentation on devices for more information about
                supported devices.


            ignore_pretraining_limits:
                Whether to ignore the pre-training limits of the model. The TabPFN
                models have been pre-trained on a specific range of input data. If the
                input data is outside of this range, the model may not perform well.
                You may ignore our limits to use the model on data outside the
                pre-training range.

                - If `True`, the model will not raise an error if the input data is
                  outside the pre-training range.
                - If `False`, you can use the model outside the pre-training range, but
                  the model could perform worse.

                !!! note

                    The current pre-training limits are:

                    - 10_000 samples/rows
                    - 500 features/columns
                    - 10 classes, this is not ignorable and will raise an error
                      if the model is used with more classes.

            inference_precision:
                The precision to use for inference. This can dramatically affect the
                speed and reproducibility of the inference. Higher precision can lead to
                better reproducibility but at the cost of speed. By default, we optimize
                for speed and use torch's mixed-precision autocast. The options are:

                - If `torch.dtype`, we force precision of the model and data to be
                  the specified torch.dtype during inference. This can is particularly
                  useful for reproducibility. Here, we do not use mixed-precision.
                - If `"autocast"`, enable PyTorch's mixed-precision autocast. Ensure
                  that your device is compatible with mixed-precision.
                - If `"auto"`, we determine whether to use autocast or not depending on
                  the device type.

            fit_mode:
                Determine how the TabPFN model is "fitted". The mode determines how the
                data is preprocessed and cached for inference. This is unique to an
                in-context learning foundation model like TabPFN, as the "fitting" is
                technically the
                forward pass of the model. The options are:

                - If `"low_memory"`, the data is preprocessed on-demand during inference
                  when calling `.predict()` or `.predict_proba()`. This is the most
                  memory-efficient mode but can be slower for large datasets because
                  the data is (repeatedly) preprocessed on-the-fly.
                  Ideal with low GPU memory and/or a single call to `.fit()` and
                  `.predict()`.
                - If `"fit_preprocessors"`, the data is preprocessed and cached once
                  during the `.fit()` call. During inference, the cached preprocessing
                  (of the training data) is used instead of re-computing it.
                  Ideal with low GPU memory and multiple calls to `.predict()` with
                  the same training data.
                - If `"fit_with_cache"`, the data is preprocessed and cached once during
                  the `.fit()` call like in `fit_preprocessors`. Moreover, the
                  transformer key-value cache is also initialized, allowing for much
                  faster inference on the same data at a large cost of memory.
                  Ideal with very high GPU memory and multiple calls to `.predict()`
                  with the same training data.

            memory_saving_mode:
                Enable GPU/CPU memory saving mode. This can help to prevent
                out-of-memory errors that result from computations that would consume
                more memory than available on the current device. We save memory by
                automatically batching certain model computations within TabPFN to
                reduce the total required memory. The options are:

                - If `bool`, enable/disable memory saving mode.
                - If `"auto"`, we will estimate the amount of memory required for the
                  forward pass and apply memory saving if it is more than the
                  available GPU/CPU memory. This is the recommended setting as it
                  allows for speed-ups and prevents memory errors depending on
                  the input data.
                - If `float` or `int`, we treat this value as the maximum amount of
                  available GPU/CPU memory (in GB). We will estimate the amount
                  of memory required for the forward pass and apply memory saving
                  if it is more than this value. Passing a float or int value for
                  this parameter is the same as setting it to True and explicitly
                  specifying the maximum free available memory.

                !!! warning
                    This does not batch the original input data. We still recommend to
                    batch this as necessary if you run into memory errors! For example,
                    if the entire input data does not fit into memory, even the memory
                    save mode will not prevent memory errors.

            random_state:
                Controls the randomness of the model. Pass an int for reproducible
                results and see the scikit-learn glossary for more information. If
                `None`, the randomness is determined by the system when calling
                `.fit()`.

                !!! warning
                    We depart from the usual scikit-learn behavior in that by default
                    we provide a fixed seed of `0`.

                !!! note
                    Even if a seed is passed, we cannot always guarantee reproducibility
                    due to PyTorch's non-deterministic operations and general numerical
                    instability. To get the most reproducible results across hardware,
                    we recommend using a higher precision as well (at the cost of a
                    much higher inference time). Likewise, for scikit-learn, consider
                    passing `USE_SKLEARN_16_DECIMAL_PRECISION=True` as kwarg.

            n_jobs:
                The number of workers for tasks that can be parallelized across CPU
                cores. Currently, this is used for preprocessing the data in parallel
                (if `n_estimators > 1`).

                - If `-1`, all available CPU cores are used.
                - If `int`, the number of CPU cores to use is determined by `n_jobs`.

            inference_config:
                For advanced users, additional advanced arguments that adjust the
                behavior of the model interface.
                See [tabpfn.constants.ModelInterfaceConfig][] for details and options.

                - If `None`, the default ModelInterfaceConfig is used.
                - If `dict`, the key-value pairs are used to update the default
                  `ModelInterfaceConfig`. Raises an error if an unknown key is passed.
                - If `ModelInterfaceConfig`, the object is used as the configuration.
                
            differentiable_input:
                If true the preprocessing will be adapted to be end-to-end differentiable
                with PyTorch. This is useful for explainability and prompt-tuning.
                
        """
        super().__init__()
        self.n_estimators = n_estimators
        self.categorical_features_indices = categorical_features_indices
        self.softmax_temperature = softmax_temperature
        self.balance_probabilities = balance_probabilities
        self.average_before_softmax = average_before_softmax
        self.model_path = model_path
        self.device = device
        self.ignore_pretraining_limits = ignore_pretraining_limits
        self.inference_precision: torch.dtype | Literal["autocast", "auto"] = (
            inference_precision
        )
        self.fit_mode: Literal["low_memory", "fit_preprocessors", "fit_with_cache"] = (
            fit_mode
        )
        self.memory_saving_mode: bool | Literal["auto"] | float | int = (
            memory_saving_mode
        )
        self.random_state = random_state
        self.n_jobs = n_jobs
        self.inference_config = inference_config
        self.differentiable_input = differentiable_input

    # TODO: We can remove this from scikit-learn lower bound of 1.6
    def _more_tags(self) -> dict[str, Any]:
        return {
            "allow_nan": True,
            "multilabel": False,
        }

    def __sklearn_tags__(self) -> Tags:
        tags = super().__sklearn_tags__()  # type: ignore
        tags.input_tags.allow_nan = True
        tags.estimator_type = "classifier"
        return tags
        
    def get_preprocessed_datasets(self, X: XType | List[XType], y: YType | List[YType] | None, 
            split_fn, batch_large_data=False, max_data_size=10000) -> Dataset:
        """ Get a torch.utils.data.Dataset which contains the different small datasets or splits of one dataset.

            Args:
                X: list of input dataset features
                y: list of input dataset labels
                split_fn: A function to dissect a dataset into train and test partition.
                batch_large_data: whether large datasets should be split up into chunks of
                    max_data_size rows.
                max_data_size: Number of chunks.
        """
        if not isinstance(X, list):
            X = [X]
            
        if not isinstance(y, list):
            y = [y]
            assert len(X) == len(y)
        
        if not hasattr(self, "model_") or self.model_ is None:
            byte_size, rng = self._initialize_model_variables()
        else:
            static_seed, rng = infer_random_state(self.random_state)
            
        if batch_large_data:
            X_split, y_split = [], []
            for (X_item, y_item) in zip(X, y): 
                Xparts, yparts = split_large_data(X_item, y_item, max_data_size)
                X_split.extend(Xparts)
                y_split.extend(yparts)
            X, y = X_split, y_split
            
        config_collection = []
        for X_item, y_item in zip(X, y):
            configs, cat_ix, X_item, y_item = self._initialize_dataset_preprocessing(X_item, y_item)
            config_collection.append([configs, X_item, y_item, cat_ix])
        meta_dataset = DatasetCollectionWithPreprocessing(split_fn, rng, config_collection)
        return meta_dataset
    
    def _initialize_model_variables(self):
        static_seed, rng = infer_random_state(self.random_state)

        # Load the model and config
        self.model_, self.config_, _ = initialize_tabpfn_model(
            model_path=self.model_path,
            which="classifier",
            fit_mode=self.fit_mode,
            static_seed=static_seed,
        )

        # Determine device and precision
        self.device_ = infer_device_and_type(self.device)
        (self.use_autocast_, self.forced_inference_dtype_, byte_size) = (
            determine_precision(self.inference_precision, self.device_)
        )

        # Build the interface_config
        self.interface_config_ = ModelInterfaceConfig.from_user_input(
            inference_config=self.inference_config,
        )

        outlier_removal_std = self.interface_config_.OUTLIER_REMOVAL_STD
        if outlier_removal_std == "auto":
            outlier_removal_std = (
                self.interface_config_._CLASSIFICATION_DEFAULT_OUTLIER_REMOVAL_STD
            )
            update_encoder_params(
                model=self.model_,
                remove_outliers_std=outlier_removal_std,
                seed=static_seed,
                inplace=True,
                differentiable_input = self.differentiable_input
            )
        return byte_size, rng

    def _initialize_dataset_preprocessing(self, X: XType, y: YType) -> List[ClassifierEnsembleConfig]:
        _, rng = infer_random_state(self.random_state)
        
        X, y, feature_names_in, n_features_in = validate_Xy_fit(
            X,
            y,
            estimator=self,
            ensure_y_numeric=False,
            max_num_samples=self.interface_config_.MAX_NUMBER_OF_SAMPLES,
            max_num_features=self.interface_config_.MAX_NUMBER_OF_FEATURES,
            ignore_pretraining_limits=self.ignore_pretraining_limits,
        )
        if feature_names_in is not None:
            self.feature_names_in_ = feature_names_in
        self.n_features_in_ = n_features_in

        # Ensure that the y values are ordinally encoded
        # TODO(eddiebergman): Ensure the counts here line up with
        #   the actual classes after label encoder.
        
        if not self.differentiable_input:
            _, counts = np.unique(y, return_counts=True)
            self.class_counts_ = counts
            self.label_encoder_ = LabelEncoder()
            y = self.label_encoder_.fit_transform(y)
            self.classes_ = self.label_encoder_.classes_  # type: ignore
            self.n_classes_ = len(self.classes_)
        else: # if pt_diffable, it is a convention that the class labels are [0, ..., n-1]
            self.label_encoder_ = None
            self.n_classes_ = int(torch.max(y).item()) + 1
            self.classes_ = torch.arange(self.n_classes_)
            
        # TODO: Support more classes with a fallback strategy.
        if self.n_classes_ > self.interface_config_.MAX_NUMBER_OF_CLASSES:
            raise ValueError(
                f"Number of classes {self.n_classes_} exceeds the maximal number of "
                "classes supported by TabPFN. Consider using a strategy to reduce "
                "the number of classes. For code see "
                "https://github.com/PriorLabs/tabpfn-extensions/blob/main/src/"
                "tabpfn_extensions/many_class/many_class_classifier.py",
            )

        # Will convert specified categorical indices to category dtype, as well
        # as handle `np.object` arrays or otherwise `object` dtype pandas columns.
        
        if not self.differentiable_input:
            X = _fix_dtypes(X, cat_indices=self.categorical_features_indices)

            # Ensure categories are ordinally encoded
            ord_encoder = _get_ordinal_encoder()
            X = ord_encoder.fit_transform(X)  # type: ignore
            assert isinstance(X, np.ndarray)
            self.preprocessor_ = ord_encoder

            cat_ix = infer_categorical_features(
                X=X,
                provided=self.categorical_features_indices,
                min_samples_for_inference=self.interface_config_.MIN_NUMBER_SAMPLES_FOR_CATEGORICAL_INFERENCE,
                max_unique_for_category=self.interface_config_.MAX_UNIQUE_FOR_CATEGORICAL_FEATURES,
                min_unique_for_numerical=self.interface_config_.MIN_UNIQUE_FOR_NUMERICAL_FEATURES,
            )
            self.inferred_categorical_indices_ = cat_ix
            preprocess_transforms = self.interface_config_.PREPROCESS_TRANSFORMS
        else: # Minimal preprocessing for prompt tuning
            self.inferred_categorical_indices_ = []
            self.preprocessor_ = None
            preprocess_transforms = [PreprocessorConfig("none", differentiable=True)]
            cat_ix = []
            
        ensemble_configs = EnsembleConfig.generate_for_classification(
            n=self.n_estimators,
            subsample_size=self.interface_config_.SUBSAMPLE_SAMPLES,
            add_fingerprint_feature=self.interface_config_.FINGERPRINT_FEATURE,
            feature_shift_decoder=self.interface_config_.FEATURE_SHIFT_METHOD,
            polynomial_features=self.interface_config_.POLYNOMIAL_FEATURES,
            max_index=len(X),
            preprocessor_configs=(
                preprocess_transforms
                if preprocess_transforms is not None
                else default_classifier_preprocessor_configs()
            ),
            class_shift_method=self.interface_config_.CLASS_SHIFT_METHOD
            if not self.differentiable_input
            else None,
            n_classes=self.n_classes_,
            random_state=rng,
        )
        assert len(ensemble_configs) == self.n_estimators
        return ensemble_configs, cat_ix, X, y
        
    def fit(self, X: XType, y: YType) -> Self:
        """Fit the model.

        Args:
            X: The input data.
            y: The target variable.
        """
        if not hasattr(self, "model_") or not self.differentiable_input:
            byte_size, rng = self._initialize_model_variables()
            self.ensemble_configs, cat_ix, X, y = self._initialize_dataset_preprocessing(X, y)
        else: #already fitted and prompt_tuning mode: no cat. features
            cat_ix = []
            _, rng = infer_random_state(self.random_state)
            _, _, byte_size = determine_precision(self.inference_precision, self.device_)
            
        # Create the inference engine
        self.executor_ = create_inference_engine(
            X_train=X,
            y_train=y,
            model=self.model_,
            ensemble_configs=self.ensemble_configs,
            cat_ix=cat_ix,
            fit_mode=self.fit_mode,
            device_=self.device_,
            rng=rng,
            n_jobs=self.n_jobs,
            byte_size=byte_size,
            forced_inference_dtype_=self.forced_inference_dtype_,
            memory_saving_mode=self.memory_saving_mode,
            use_autocast_=self.use_autocast_,
            inference_mode = not self.differentiable_input
        )

        return self

    def fit_from_preprocessed(self, X_preprocessed: List[List[torch.Tensor]],
                              y_preprocessed: List[List[torch.Tensor]], 
                              cat_ix: List[List[List[int]]],
                              configs: List[List[EnsembleConfig]],
                              padding_val: float = 0.0, no_refit=True) -> TabPFNClassifier:
        """Fit the model.

        Args:
            X: The input features obtained from the preprocessed Dataset
            y: The target variable obtained from the preproecessed Dataset
            cat_ix: categorical indices obtained from the preprocessed Dataset
            config: Ensemble configurations obtained from the preprocessed Dataset
            padding_val: value used to pad datasets with different amount of features or samples
            no_refit: if True, the classifier will not be reinitialized when calling fit multiple times.
        """
        # If there isa model, and we are lazy, we skip reinitialization
        if not hasattr(self, "model_") or not no_refit: 
            byte_size, rng = self._initialize_model_variables()
        else:
            _, _, byte_size = determine_precision(self.inference_precision, self.device_)
            
        # Create the inference engine
        self.executor_ = create_inference_engine(
            X_train=X_preprocessed,
            y_train=y_preprocessed,
            model=self.model_,
            ensemble_configs=configs,
            cat_ix=cat_ix,
            fit_mode="batched",
            device_=self.device_,
            rng=None,
            n_jobs=self.n_jobs,
            byte_size=byte_size,
            forced_inference_dtype_=self.forced_inference_dtype_,
            memory_saving_mode=self.memory_saving_mode,
            use_autocast_=self.use_autocast_,
            inference_mode = not self.differentiable_input,
            padding_val_= padding_val
        )

        return self

    def predict(self, X: XType) -> np.ndarray:
        """Predict the class labels for the provided input samples.

        Args:
            X: The input samples.

        Returns:
            The predicted class labels.
        """
        proba = self.predict_proba(X)
        y = np.argmax(proba, axis=1)
        if self.label_encoder_:
            return self.label_encoder_.inverse_transform(y)  # type: ignore
        else:
            return y
        
    def predict_proba(self, X: XType) -> np.ndarray:
        """Predict the probabilities of the classes for the provided input samples.

        Args:
            X: The input data.

        Returns:
            The predicted probabilities of the classes.
        """
        check_is_fitted(self)

        if not self.differentiable_input:
            X = validate_X_predict(X, self)
            X = _fix_dtypes(X, cat_indices=self.categorical_features_indices)
            X = self.preprocessor_.transform(X)

        output = self.predict_proba_tensor(X)
            
        output = output.float().detach().cpu().numpy()

        if self.interface_config_.USE_SKLEARN_16_DECIMAL_PRECISION:
            output = np.around(output, decimals=SKLEARN_16_DECIMAL_PRECISION)
            output = np.where(output < PROBABILITY_EPSILON_ROUND_ZERO, 0.0, output)

        # Normalize to guarantee proba sum to 1, required due to precision issues and
        # going from torch to numpy
        return output / output.sum(axis=1, keepdims=True)  # type: ignore
        
    def predict_proba_from_preprocessed(self, X: List[List[torch.Tensor]]) -> torch.Tensor:
        """Predict the probabilities of the classes for the provided input samples
        Different that the main predict proba function, this interface allows to backpropagate through 
        the tensors which can be used to fine-tune or prompt-tune the model.

        Args:
            X: The input data.

        Returns:
            The predicted probabilities of the classes.
        """
        if not isinstance(self.executor_, InferenceEngineBatchedNoPreprocessing):
            raise ValueError("Error using batched mode: \
                predict_proba_from_preprocessed can only be called \
                following fit_from_preprocessed.")
            
        self.executor_.use_torch_inference_mode(False)
        outputs = []
        for output, config in self.executor_.iter_outputs(
            X,
            device=self.device_,
            autocast=self.use_autocast_,
        ):
            # Cut out logits for classes which do not exist
            assert output.ndim == 3  # [Batch, Nsamples, NClasses]
            n_classes = output.size(-1)
            if self.softmax_temperature != 1:
                output = (  # noqa: PLW2901
                    output[:, :, :n_classes].float() / self.softmax_temperature
                )
                
            if config is not None:
                output_batch = []
                for i, batch_config in enumerate(config):
                    output_batch.append(output[:, i, batch_config.class_permutation])  # noqa: PLW2901
                output_all = torch.stack(output_batch, dim=1)
            outputs.append(output_all)
        
        if self.average_before_softmax:
            output = torch.stack(outputs).mean(dim=0)
            output = torch.nn.functional.softmax(output, dim=-1)
        else:
            # Softmax each 2d outputs before average
            outputs = [torch.nn.functional.softmax(o, dim=-1) for o in outputs]
            output = torch.stack(outputs).mean(dim=0)
        
        if self.balance_probabilities:
            class_prob_in_train = self.class_counts_ / self.class_counts_.sum()
            output = output / torch.Tensor(class_prob_in_train).to(self.device_)
            output = output / output.sum(dim=-1, keepdim=True)
            
        return output.transpose(0, 1).transpose(1, 2) # for NLLLoss [B, C, D1]
        
    def predict_proba_tensor(self, X: torch.Tensor) -> torch.Tensor:
        """ Same as predict_proba, but without preprocessing and with
            Tensor inputs and outputs. Use, e.g., for prompt-tuning or
            or when the outputs need to be differentiable.
        """
        outputs: list[torch.Tensor] = []
        if isinstance(self.executor_, InferenceEngineBatchedNoPreprocessing):
            raise ValueError("Error using predict_proba: \
                If you use fit_from_preprocessed use \
                predict_proba_from_preprocessed for following inferences.")
                
        for output, config in self.executor_.iter_outputs(
            X,
            device=self.device_,
            autocast=self.use_autocast_,
        ):
            assert isinstance(config, ClassifierEnsembleConfig)
            # Cut out logits for classes which do not exist
            assert output.ndim == 2

            if self.softmax_temperature != 1:
                output = (  # noqa: PLW2901
                    output[:, :self.n_classes_].float() / self.softmax_temperature
                )

            # Reverse class permutation if exists
            if config.class_permutation is not None:
                output = output[..., config.class_permutation]  # noqa: PLW2901

            outputs.append(output)

        if self.average_before_softmax:
            output = torch.stack(outputs).mean(dim=0)
            output = torch.nn.functional.softmax(output, dim=1)
        else:
            # Softmax each 2d outputs before average
            outputs = [torch.nn.functional.softmax(o, dim=1) for o in outputs]
            output = torch.stack(outputs).mean(dim=0)

        if self.balance_probabilities:
            class_prob_in_train = self.class_counts_ / self.class_counts_.sum()
            output = output / torch.Tensor(class_prob_in_train).to(self.device_)
            output = output / output.sum(dim=-1, keepdim=True)
        
        return output

    def get_embeddings(
        self,
        X: XType,
        data_source: Literal["train", "test"] = "test",
    ) -> np.ndarray:
        """Get the embeddings for the input data `X`.

        Parameters:
            X (XType): The input data.
            data_source str: Extract either the train or test embeddings
        Returns:
            np.ndarray: The computed embeddings for each fitted estimator.
        """
        return _get_embeddings(self, X, data_source)
