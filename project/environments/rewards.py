# rewards.py: Reward functions
#
# (C) 2019, Daniel Mouritzen

from math import inf
from typing import Dict, Iterable, Optional, Tuple

import gin
import gym
import numpy as np

Observations = Dict[str, np.ndarray]


class RewardFunction:
    def __init__(self) -> None:
        self._env: Optional[gym.Env] = None

    def set_env(self, env: gym.Env) -> None:
        self._env = env

    def get_reward_range(self) -> Tuple[float, float]:
        raise NotImplementedError

    def get_reward(self, observations: Observations) -> float:
        raise NotImplementedError

    def reset(self) -> None:
        pass

    def assert_env(self) -> None:
        assert self._env is not None, 'set_env has not been called yet!'


class CombinedRewards(RewardFunction):
    def __init__(self, rewards: Iterable[RewardFunction]) -> None:
        super().__init__()
        self.rewards = rewards

    def set_env(self, env: gym.Env) -> None:
        super().set_env(env)
        for reward in self.rewards:
            reward.set_env(env)

    def get_reward_range(self) -> Tuple[float, float]:
        lower, upper = 0.0, 0.0
        for a, b in [reward.get_reward_range() for reward in self.rewards]:
            lower += a
            upper += b
        return lower, upper

    def get_reward(self, observations: Observations) -> float:
        return sum(reward.get_reward(observations) for reward in self.rewards)

    def reset(self) -> None:
        for reward in self.rewards:
            reward.reset()


@gin.configurable(whitelist=['rewards'])
def combine_rewards(rewards: Iterable[RewardFunction]) -> CombinedRewards:
    return CombinedRewards(rewards)


class DenseReward(RewardFunction):
    """
    Implements a dense reward function based on
    github.com/facebookresearch/habitat-api/blob/master/habitat_baselines/common/environments.py
    """
    def __init__(self, slack_reward: float, success_reward: float, distance_scaling: float) -> None:
        # assert all(hasattr(env, name) for name in ('distance_to_target', 'episode_success', 'habitat_env')), \
        #        'Env must be a Habitat environment!'
        super().__init__()
        self._slack_reward = slack_reward
        self._success_reward = success_reward
        self._distance_scaling = distance_scaling
        self._previous_target_distance: Optional[float] = None

    def get_reward_range(self) -> Tuple[float, float]:
        self.assert_env()
        step_size: float = self._env.sim.config.FORWARD_STEP_SIZE  # type: ignore[union-attr]
        return self._slack_reward - step_size, self._success_reward + step_size

    def get_reward(self, observations: Observations) -> float:
        self.assert_env()
        if self._previous_target_distance is None:
            # New episode
            self._previous_target_distance = self._env.habitat_env.current_episode.info["geodesic_distance"]  # type: ignore[union-attr]

        reward = self._slack_reward

        current_target_distance: float = self._env.distance_to_target()  # type: ignore[union-attr]
        reward += self._distance_scaling * (self._previous_target_distance - current_target_distance)
        self._previous_target_distance = current_target_distance

        if self._env.episode_success():  # type: ignore[union-attr]
            reward += self._success_reward

        return reward

    def reset(self) -> None:
        self._previous_target_distance = None


@gin.configurable(whitelist=['slack_reward', 'success_reward', 'distance_scaling'])
def dense_reward(slack_reward: float = -0.01,
                 success_reward: float = 10.0,
                 distance_scaling: float = 1.0) -> DenseReward:
    return DenseReward(slack_reward, success_reward, distance_scaling)


class OptimalPathLengthReward(RewardFunction):
    """Implements a sparse reward proportional to the optimal path length, as in arxiv.org/abs/1804.00168"""
    def __init__(self, scaling: float) -> None:
        # assert all(hasattr(env, name) for name in ('episode_success', 'habitat_env')), \
        #        'Env must be a Habitat environment!'
        super().__init__()
        self._scaling = scaling

    def get_reward_range(self) -> Tuple[float, float]:
        return 0.0, inf

    def get_reward(self, observations: Observations) -> float:
        self.assert_env()
        if self._env.episode_success():  # type: ignore[union-attr]
            optimal_path_length: float = self._env.habitat_env.current_episode.info["geodesic_distance"]  # type: ignore[union-attr]
            return self._scaling * optimal_path_length
        return 0.0


@gin.configurable(whitelist=['scaling'])
def optimal_path_length_reward(scaling: float = 1.0) -> OptimalPathLengthReward:
    return OptimalPathLengthReward(scaling)


class CollisionPenalty(RewardFunction):
    """Adds a penalty for colliding with an obstacle."""
    def __init__(self, scaling: float) -> None:
        # assert hasattr(env, 'sim'), 'Env must be a Habitat environment!'
        super().__init__()
        self._scaling = scaling

    def get_reward_range(self) -> Tuple[float, float]:
        return -self._scaling, 0.0

    def get_reward(self, observations: Observations) -> float:
        self.assert_env()
        if self._env.sim.previous_step_collided:  # type: ignore[union-attr]
            return -self._scaling
        return 0.0


@gin.configurable(whitelist=['scaling'])
def collision_penalty(scaling: float = 1.0) -> CollisionPenalty:
    return CollisionPenalty(scaling)


class ObstacleDistancePenalty(RewardFunction):
    """
    Adds a penalty for getting closer than `threshold` meters from an obstacle, taking the agent's radius into account.
    The penaly increases linearly up to `scaling`.
    """
    def __init__(self, threshold: float, scaling: float) -> None:
        # assert hasattr(env, 'sim'), 'Env must be a Habitat environment!'
        super().__init__()
        self._threshold = threshold
        self._scaling = scaling

    def get_reward_range(self) -> Tuple[float, float]:
        return -self._scaling, 0.0

    def get_reward(self, observations: Observations) -> float:
        self.assert_env()
        distance: float = self._env.sim.distance_to_closest_obstacle(  # type: ignore[union-attr]
            self._env.sim.get_agent_state().position, self._threshold) - self._env.sim.config.AGENT_0.RADIUS  # type: ignore[union-attr]
        if distance < self._threshold:
            return self._scaling * (distance / self._threshold - 1.0)
        return 0.0


@gin.configurable(whitelist=['threshold', 'scaling'])
def obstacle_distance_penalty(threshold: float = 1.0, scaling: float = 1.0) -> ObstacleDistancePenalty:
    return ObstacleDistancePenalty(threshold, scaling)
