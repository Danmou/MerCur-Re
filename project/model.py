# model.py: Provides Model class
#
# (C) 2019, Daniel Mouritzen

import pickle
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Type, cast

import gin
import tensorflow as tf
from loguru import logger

from project import networks
from project.util.files import get_latest_checkpoint
from project.util.system import is_debugging
from project.util.tf import auto_shape, losses
from project.util.timing import measure_time


@gin.configurable(whitelist=['predictor_class', 'rnn_class', 'disable_tf_optimization'])
class Model(auto_shape.Model):
    """This class defines the top-level model structure and losses"""
    def __init__(self,
                 observation_components: Iterable[str],
                 data_spec: Mapping[str, tf.TensorSpec],
                 predictor_class: Type[networks.predictors.Predictor] = gin.REQUIRED,
                 rnn_class: Type[networks.rnns.RNN] = gin.REQUIRED,
                 disable_tf_optimization: bool = False,
                 ) -> None:
        super().__init__(batch_dims=2)
        self._observation_components = list(observation_components)
        self._data_spec = data_spec
        self._batch_size = next(iter(data_spec.values())).shape[0]

        if disable_tf_optimization or is_debugging():
            logger.warning('Running without tf.function optimization.')
            tf.config.experimental_run_functions_eagerly(True)
            self.run_eagerly = True

        additional_observations = set(observation_components) - {'image'}
        self.encoder = networks.SelectItems(
            networks.ExtraBatchDim(networks.Encoder(image_input='image',
                                                    vector_inputs=additional_observations,
                                                    name='image_encoder')),
            keys=list(observation_components)
        )
        self.decoders = {'image': networks.ExtraBatchDim(networks.Decoder(), name='image_decoder')}
        for key in sorted(additional_observations | {'reward'}):
            data_shape = data_spec[key].shape[2:].as_list()
            self.decoders[key] = self._get_vector_decoder(data_shape,
                                                          name=f'{key}_decoder')
        # Layers in a dict are not automatically tracked, so we add them manually
        for layer in self.decoders.values():
            self._layers.append(layer)
        self._track_layers(self._layers)
        self.rnn = rnn_class(predictor_class)

    @staticmethod
    @gin.configurable('Model.decoders', whitelist=['num_units', 'num_layers', 'activation'])
    def _get_vector_decoder(output_shape: Sequence[int],
                            num_units: int = gin.REQUIRED,
                            num_layers: int = gin.REQUIRED,
                            activation: str = 'relu',
                            name: str = 'vector_encoder'
                            ) -> auto_shape.Layer:
        return networks.ExtraBatchDim(auto_shape.Sequential([networks.SequentialBlock(num_units=num_units,
                                                                                      num_layers=num_layers,
                                                                                      activation=activation,
                                                                                      name=f'{name}_block'),
                                                             networks.ShapedDense(output_shape,
                                                                                  activation=None,
                                                                                  name=f'{name}_shaped_dense')],
                                                            name=f'{name}_sequential'),
                                      name=name)

    @staticmethod
    def _get_mask(data: Mapping[str, tf.Tensor]) -> tf.Tensor:
        return tf.sequence_mask(data['length'], tf.shape(data['reward'])[1])

    @property
    def dummy_data(self) -> Dict[str, tf.Tensor]:
        """Create dummy data suitable for initializing the model's weights"""
        data = {}
        for key in self._data_spec.keys():
            if key != 'length':
                data[key] = tf.zeros(self._data_spec[key].shape, self._data_spec[key].dtype)
        batch_shape = data['image'].shape[:2]
        data['length'] = tf.constant([[batch_shape[1]]] * batch_shape[0], self._data_spec['length'].dtype)
        return data

    def closed_loop(self, data: Mapping[str, tf.Tensor]) -> Tuple[Tuple[tf.Tensor, ...], Tuple[tf.Tensor, ...]]:
        embedded = self.encoder(data)
        prior, posterior = self.rnn.closed_loop(embedded, data['action'], mask=self._get_mask(data))
        return prior, posterior

    @gin.configurable(whitelist=['context'])
    def open_loop(self, data: Mapping[str, tf.Tensor], context: int = 5) -> Tuple[tf.Tensor, ...]:
        embedded = self.encoder(data)
        mask = self._get_mask(data)
        context = min(mask.shape[1] - 1, context)
        _, closed_loop = self.rnn.closed_loop(embedded[:, :context], data['action'][:, :context], mask=mask[:, :context])
        last_posterior = tf.nest.map_structure(lambda x: x[:, -1], closed_loop)
        open_loop = self.rnn.open_loop(data['action'][:, context:], initial_state=last_posterior, mask=mask[:, context:])
        return cast(Tuple[tf.Tensor, ...], tf.nest.map_structure(lambda x, y: tf.concat([x, y], 1), closed_loop, open_loop))

    def decode(self, state_features: tf.Tensor) -> Dict[str, tf.Tensor]:
        reconstructions = {}
        for name, decoder in self.decoders.items():
            reconstructions[name] = decoder(state_features)
        return reconstructions

    def call(self, inputs: Mapping[str, tf.Tensor]) -> tf.Tensor:
        inputs = dict(inputs)  # Shallow copy input dict so we can modify it safely
        if self._batch_size and tf.nest.flatten(inputs)[0].shape[0] is None:
            # Workaround for keras making the batch dimension undefined
            tf.nest.map_structure(lambda x: x.set_shape([self._batch_size] + x.shape[1:]), inputs)
        if inputs['length'].shape.ndims > 1:
            inputs['length'] = inputs['length'][:, 0]
        prior, posterior = self.closed_loop(inputs)
        reconstructions = self.decode(self.rnn.state_to_features(posterior))
        mask = self._get_mask(inputs)
        losses = {'divergence': self.divergence_loss(prior, posterior, mask)}
        losses.update(self.reconstruction_log_probs(inputs, reconstructions, mask))
        combined_loss = self.combine_losses(losses)
        self.add_loss(combined_loss, inputs=True)
        self.add_metric(combined_loss, aggregation='mean', name='loss')
        for name, loss in losses.items():
            self.add_metric(loss, aggregation='mean', name=name)
        # return prior, posterior, open_loop, reconstructions
        return tf.constant(0.0)

    @gin.configurable(whitelist=['free_nats'])
    @tf.function(experimental_relax_shapes=True)
    def divergence_loss(self,
                        prior: Tuple[tf.Tensor, ...],
                        posterior: Tuple[tf.Tensor, ...],
                        mask: Optional[tf.Tensor] = None,
                        free_nats: float = 3.0,
                        ) -> tf.Tensor:
        divergence_loss = self.rnn.state_divergence(posterior, prior, mask)
        if free_nats:
            divergence_loss = tf.maximum(0.0, divergence_loss - float(free_nats))
        if mask is not None:
            divergence_loss = tf.boolean_mask(divergence_loss, mask)
        if divergence_loss.shape[0] == 0:
            divergence_loss = tf.constant(0.0)
        return tf.reduce_mean(divergence_loss, name='divergence_loss')

    @tf.function(experimental_relax_shapes=True)
    def reconstruction_log_probs(self,
                                 targets: Mapping[str, tf.Tensor],
                                 reconstructions: Mapping[str, tf.Tensor],
                                 mask: Optional[tf.Tensor] = None,
                                 ) -> Dict[str, tf.Tensor]:
        log_probs = {}
        for name, reconstruction in reconstructions.items():
            target = targets[name]
            log_prob = losses.mse(reconstruction, target, batch_dims=2)
            if mask is not None:
                log_prob = tf.boolean_mask(log_prob, mask)
            if log_prob.shape[0] == 0:
                log_prob = tf.constant(0.0)
            log_probs[f'{name}_reconstruction'] = tf.reduce_mean(log_prob, name=f'{name}_reconstruction_loss')
        return log_probs

    @gin.configurable(whitelist=['scales'])
    def combine_losses(self,
                       all_losses: Mapping[str, tf.Tensor],
                       scales: Mapping[str, float] = gin.REQUIRED,
                       ) -> tf.Tensor:
        total = 0.0
        for name, loss in all_losses.items():
            scale = scales.get(name, 0.0)
            if not scale:
                continue
            total += scale * loss
        return tf.identity(total, name='total_loss')

    def save_weights(self, filepath: str, **kwargs: Any) -> None:
        super().save_weights(filepath, **kwargs)
        path = Path(filepath)
        additional_data_file = path.parent / 'checkpoint_additional_data.pickle'
        additional_data = {'observation_components': self._observation_components,
                           'data_spec': self._data_spec}
        with open(additional_data_file, 'wb') as f1:
            pickle.dump(additional_data, f1, pickle.HIGHEST_PROTOCOL)
        latest_checkpoint_file = path.parent / 'checkpoint_latest'
        with open(latest_checkpoint_file, 'w') as f2:
            f2.write(path.name)


@measure_time
@gin.configurable(whitelist=['optimizer'])
def get_model(observation_components: Iterable[str],
              data_spec: Mapping[str, tf.TensorSpec],
              optimizer: tf.keras.optimizers.Optimizer,
              ) -> Model:
    """Returns a built model with random weights"""
    logger.info(f'Building model...')
    model = Model(observation_components, data_spec)
    model.compile(optimizer=optimizer, loss=None)
    model.build_with_input(model.dummy_data)  # Initialize weights
    model.reset_metrics()
    return model


def restore_model(checkpoint: Path, base_dir: Optional[Path] = None) -> Tuple[Model, int]:
    """Returns a fully initialized model and the index of the next epoch"""
    checkpoint = get_latest_checkpoint(checkpoint, base_dir)
    try:
        epoch = int(re.findall(r'epoch_(\d+)', checkpoint.name)[0]) + 1
    except (IndexError, ValueError):
        logger.warning(f"Could't read epoch from checkpoint name {checkpoint.name}")
        epoch = 0
    additional_data_file = checkpoint.parent / 'checkpoint_additional_data.pickle'
    if not additional_data_file.is_file():
        raise ValueError(f'Additional data not found, should be {additional_data_file}')
    with open(additional_data_file, 'rb') as f:
        additional_data = pickle.load(f)
    model = get_model(**additional_data)
    logger.info(f'Restoring weights from {checkpoint}...')
    measure_time(model.load_weights)(str(checkpoint))
    return model, epoch
