# callbacks.py: Callback classes for use with tf.keras
#
# (C) 2019, Daniel Mouritzen

import time
from functools import partial
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional, SupportsFloat, Tuple, Union

import gin
import matplotlib.pyplot as plt
import tensorflow as tf
import wandb
from loguru import logger
from tensorflow.keras import callbacks

from project.agents import Agent
from project.execution import Evaluator, Simulator
from project.model import Model
from project.util import PrettyPrinter, Statistics
from project.util.planet.numpy_episodes import episode_reader
from project.util.planet.preprocess import postprocess, preprocess
from project.util.system import get_memory_usage
from project.util.tf.summaries import prediction_trajectory_summary, video_summary
from project.util.timing import measure_time


@gin.configurable(whitelist=['period'])
class CheckpointCallback(callbacks.ModelCheckpoint):
    """Saves a checkpoint every `period` epochs"""
    def __init__(self, *args: Any, period: int = 1, **kwargs: Any) -> None:
        kwargs['save_freq'] = 'epoch'
        super().__init__(*args, **kwargs)
        self.period = period


@gin.configurable(whitelist=['period', 'train_episodes', 'test_episodes'])
class DataCollectionCallback(callbacks.Callback):
    """Collects new data between epochs using current model"""
    def __init__(self,
                 sims: Mapping[str, Simulator],
                 agents: Mapping[str, Agent],
                 dirs: Mapping[str, Path],
                 period: int = 1,
                 train_episodes: int = 1,
                 test_episodes: int = 1,
                 ) -> None:
        super().__init__()
        self._sims = sims
        self._agents = agents
        self._dirs = dirs
        self._period = period
        self._num_episodes = {'train': train_episodes, 'test': test_episodes}

    def on_epoch_end(self, epoch: int, logs: Any = None) -> None:
        if self._period and (epoch + 1) % self._period == 0:
            for task, sim in self._sims.items():
                mean_metrics: Mapping[str, float] = {}
                total_episodes = 0
                for phase, episodes in self._num_episodes.items():
                    logger.info(f'Collecting {episodes} on-policy episodes ({task} {phase}).')
                    count_seen = phase == 'train'
                    metrics = sim.run(self._agents[task],
                                      episodes,
                                      save_dir=self._dirs[phase],
                                      save_data=True,
                                      count=count_seen)
                    mean_metrics = {k: mean_metrics.get(k, 0) + episodes * v for k, v in metrics.items()}
                    total_episodes += episodes
                    if count_seen:
                        wandb.log({f'{task}/{phase}/steps_seen': sim.steps_seen,
                                   f'{task}/{phase}/scenes_seen': sim.scenes_seen}, step=epoch)
                mean_metrics = {k: v / total_episodes for k, v in mean_metrics.items()}
                wandb.log({f'{task}/train/{k}': v for k, v in mean_metrics.items()}, step=epoch)


@gin.configurable(whitelist=['period', 'num_episodes'])
class EvaluateCallback(callbacks.Callback):
    """Evaluates current model"""
    def __init__(self,
                 evaluator: Evaluator,
                 agents: Mapping[str, Agent],
                 period: int = 10,
                 num_episodes: int = 10,
                 ) -> None:
        super().__init__()
        self._evaluator = evaluator
        self._agents = agents
        self._period = period
        self._num_episodes = num_episodes

    def on_epoch_end(self, epoch: int, logs: Any = None) -> None:
        if self._period and (epoch + 1) % self._period == 0:
            metrics, videos = self._evaluator.evaluate(agents=self._agents,
                                                       num_episodes=self._num_episodes,
                                                       seed=None,
                                                       sync_wandb=True)
            wandb.log({f'{task}/eval/{k}': v
                       for task, task_metrics in metrics.items()
                       for k, v in task_metrics.items()},
                      step=epoch)
            if videos:
                wandb.log({f'{task}/eval/video_{i}': v
                           for task, task_videos in videos.items()
                           for i, v in enumerate(task_videos)},
                          step=epoch)


@gin.configurable(whitelist=['epoch_log_period', 'epoch_header_period', 'batch_log_period', 'batch_header_period'])
class LoggingCallback(callbacks.Callback):
    """Logs metrics"""
    def __init__(self,
                 epoch_log_period: int = 1,
                 epoch_header_period: int = 5,
                 batch_log_period: int = 10,
                 batch_header_period: int = 10,
                 ) -> None:
        super().__init__()
        self._epoch_log_period = epoch_log_period
        self._epoch_header_period = epoch_header_period * epoch_log_period
        self._epoch_printer: Optional[PrettyPrinter] = None
        self._epoch_statistics: Optional[Statistics] = None
        self._batch_log_period = batch_log_period
        self._batch_header_period = batch_header_period * batch_log_period
        self._batch_printer: Optional[PrettyPrinter] = None
        self._batch_statistics: Optional[Statistics] = None
        self._prev_time = time.time()
        self._steps = 0

    def on_train_batch_end(self, batch: int, logs: Optional[Mapping[str, SupportsFloat]] = None) -> None:
        if logs is None:
            return
        log_batch = batch + 1  # Count batches from 1
        keys = list(logs.keys())
        if 'batch' in keys:
            keys.remove('batch')
        if 'size' in keys:
            keys.remove('size')
        if self._batch_printer is None:
            self._batch_printer = PrettyPrinter(['batch'] + keys, log_fn=logger.debug)
        if self._batch_statistics is None:
            self._batch_statistics = Statistics(keys)
        self._batch_statistics.update(logs)
        if self._batch_header_period and batch % self._batch_header_period == 0:
            self._batch_printer.print_header()
        if self._batch_log_period and log_batch % self._batch_log_period == 0:
            row = self._batch_statistics.mean
            row['batch'] = log_batch
            self._batch_printer.print_row(row)
            self._batch_statistics.reset()
        self._steps += 1

    def on_epoch_end(self, epoch: int, logs: Optional[Mapping[str, SupportsFloat]] = None) -> None:
        if logs is None:
            return
        log_epoch = epoch + 1  # Count epochs from 1
        if self._epoch_printer is None:
            self._epoch_printer = PrettyPrinter(['epoch', 'phase'] + [k for k in logs.keys() if not k.startswith('val_')],
                                                log_fn=logger.info)
        if self._epoch_statistics is None:
            self._epoch_statistics = Statistics(logs.keys())
        self._epoch_statistics.update(logs)
        if self._epoch_header_period and epoch % self._epoch_header_period == 0:
            self._epoch_printer.print_header()
        if self._epoch_log_period and log_epoch % self._epoch_log_period == 0:
            row: MutableMapping[str, Union[str, SupportsFloat]] = self._epoch_statistics.mean  # type: ignore[assignment]  # mypy/issues/8136
            row['epoch'] = log_epoch
            row['phase'] = 'train'
            self._epoch_printer.print_row(row)
            row = {k[4:]: v for k, v in row.items() if k.startswith('val_')}
            if row:
                row['epoch'] = log_epoch
                row['phase'] = 'val'
                self._epoch_printer.print_row(row)
            self._epoch_statistics.reset()
        wandb_row = {f'train/{k}': v for k, v in logs.items() if not k.startswith('val_')}
        wandb_row.update({f'val/{k[4:]}': v for k, v in logs.items() if k.startswith('val_')})
        current_time = time.time()
        epoch_time = current_time - self._prev_time
        wandb_row['epoch_time'] = epoch_time
        wandb_row['step_time'] = epoch_time / self._steps
        wandb_row['steps'] = log_epoch * self._steps
        wandb_row['memory'] = get_memory_usage()
        self._prev_time = current_time
        self._steps = 0
        wandb.log(wandb_row, step=epoch)


class WandbCommitCallback(callbacks.Callback):
    """Simply makes wandb upload the metrics immediately at the end of the epoch. This callback should be last."""
    def on_epoch_end(self, epoch: int, logs: Optional[Mapping[str, SupportsFloat]] = None) -> None:
        wandb.log()


Episode = Dict[str, tf.Tensor]


@gin.configurable(whitelist=['period', 'batch_episodes'])
class PredictionSummariesCallback(callbacks.Callback):
    """Summaries visualizing the output of the model"""
    def __init__(self, model: Model, dirs: Mapping[str, Path], period: int = 10, batch_episodes: int = 5) -> None:
        super().__init__()
        self._model = model
        self._period = period
        self._batch_episodes = batch_episodes
        self._episode_getters = {f'{phase}/{"no_"*(not success)}success': partial(self._get_episodes, directory, success)
                                 for success in [False, True]
                                 for phase, directory in dirs.items()}
        self._episodes: Dict[str, Optional[Episode]] = {k: None for k in self._episode_getters.keys()}

    def _get_episodes(self, directory: Path, success: Optional[bool] = None) -> Optional[Episode]:
        episodes = []
        for episode_file in sorted(directory.glob('*.npz')):
            episode = episode_reader(str(episode_file))
            if success is None or bool(episode['success'][-1]) == success:
                episode = {k: tf.convert_to_tensor(v) for k, v in episode.items()}
                episode['image'] = preprocess(episode['image'])
                episode['length'] = tf.convert_to_tensor(episode['reward'].shape[0])
                episodes.append(episode)
            if len(episodes) >= self._batch_episodes:
                break
        if not episodes:
            return None
        length = max([ep['length'] for ep in episodes])
        episodes = [self._pad_episode(ep, length) for ep in episodes]
        return {k: tf.stack([ep[k] for ep in episodes]) for k in episodes[0].keys()} if episodes else None

    @staticmethod
    def _pad_episode(episode: Episode, length: tf.Tensor) -> Episode:
        amount = length - episode['length']
        output = episode.copy()
        if amount > 0:
            for key, value in episode.items():
                if key != 'length':
                    padding = tf.tile(value[tf.newaxis, -1], [amount] + [1] * (value.shape.ndims - 1))
                    output[key] = tf.concat([value, padding], 0)
            output['length'] = length
        return output

    @staticmethod
    def _postprocess_images(images: tf.Tensor) -> tf.Tensor:
        return tf.clip_by_value(postprocess(images), 0.0, 1.0)

    def _get_reconstructions(self, states: Tuple[tf.Tensor, ...]) -> Dict[str, tf.Tensor]:
        reconstructions = self._model.decode(self._model.rnn.state_to_features(states), training=False)
        reconstructions['image'] = self._postprocess_images(reconstructions['image'])
        return reconstructions

    @measure_time(name='prediction_summaries')
    def _make_summaries(self, base_name: str) -> Dict[str, wandb.data_types.WBValue]:
        episode_batch = self._episodes[base_name]
        if not episode_batch:
            return {}
        summaries = {}
        prior, posterior = self._model.closed_loop(episode_batch, training=False)
        open_loop = self._model.open_loop(episode_batch, training=False)
        target_images = self._postprocess_images(episode_batch['image'])
        for name, states in [('closed_loop/prior', prior), ('closed_loop/posterior', posterior), ('open_loop', open_loop)]:
            summaries[f'{base_name}/{name}'] = video_summary(target_images, self._get_reconstructions(states)['image'])
        open_loop_predictions = self._get_reconstructions(open_loop)
        for key in list(open_loop_predictions.keys()) + ['action', 'taken_action']:
            if key == 'image' or key not in episode_batch:
                continue
            # We only look at the first example in the batch.
            prediction = open_loop_predictions[key][0] if key in open_loop_predictions else None
            target = episode_batch[key][0]
            summaries[f'{base_name}/{key}_trajectory'] = prediction_trajectory_summary(target, prediction, key)
        return summaries

    def on_epoch_end(self, epoch: int, logs: Mapping[str, SupportsFloat] = None) -> None:
        if self._period and (epoch + 1) % self._period == 0:
            plt.close('all')
            for name in self._episodes.keys():
                episode = self._episodes[name]
                if episode is None or episode['reward'].shape[0] < self._batch_episodes:
                    self._episodes[name] = self._episode_getters[name]()
                wandb.log(self._make_summaries(name), step=epoch)
