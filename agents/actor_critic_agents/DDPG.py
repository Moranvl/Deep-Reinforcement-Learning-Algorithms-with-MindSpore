"""
DDPG
"""
import mindspore as ms
from mindspore import nn, ops
from agents.Base_Agent import Base_Agent
from utilities.data_structures.Replay_Buffer import Replay_Buffer
from exploration_strategies.OU_Noise_Exploration import OU_Noise_Exploration


class DDPG(Base_Agent):
    """A DDPG Agent"""
    agent_name = "DDPG"

    def __init__(self, config):
        Base_Agent.__init__(self, config)
        self.hyperparameters = config.hyperparameters
        self.critic_local = self.create_NN(
            input_dim=self.state_size + self.action_size, output_dim=1, key_to_use="Critic"
        )
        self.critic_target = self.create_NN(
            input_dim=self.state_size + self.action_size, output_dim=1, key_to_use="Critic"
        )
        Base_Agent.copy_model_over(self.critic_local, self.critic_target)

        self.critic_optimizer = nn.Adam(
            self.critic_local.trainable_params(),
            learning_rate=self.hyperparameters["Critic"]["learning_rate"],
            eps=1e-4
        )
        self.memory = Replay_Buffer(self.hyperparameters["Critic"]["buffer_size"], self.hyperparameters["batch_size"],
                                    self.config.seed)
        self.actor_local = self.create_NN(input_dim=self.state_size, output_dim=self.action_size, key_to_use="Actor")
        self.actor_target = self.create_NN(input_dim=self.state_size, output_dim=self.action_size, key_to_use="Actor")
        Base_Agent.copy_model_over(self.actor_local, self.actor_target)

        self.actor_optimizer = nn.Adam(
            self.actor_local.trainable_params(),
            learning_rate=self.hyperparameters["Actor"]["learning_rate"],
            eps=1e-4
        )
        self.exploration_strategy = OU_Noise_Exploration(self.config)

        # grads
        self.critic_grad_fn = ms.value_and_grad(
            self.compute_loss, grad_position=None, weights=self.critic_local.trainable_params(), has_aux=False
        )
        self.actor_grad_fn = ms.value_and_grad(
            self.calculate_actor_loss, grad_position=None, weights=self.actor_local.trainable_params(), has_aux=False
        )

    def step(self):
        """Runs a step in the game"""
        while not self.done:
            # print("State ", self.state.shape)
            self.action = self.pick_action()
            self.conduct_action(self.action)
            if self.time_for_critic_and_actor_to_learn():
                for _ in range(self.hyperparameters["learning_updates_per_learning_session"]):
                    states, actions, rewards, next_states, dones = self.sample_experiences()
                    self.critic_learn(states, actions, rewards, next_states, dones)
                    self.actor_learn(states)
            self.save_experience()
            # this is to set the state for the next iteration
            self.state = self.next_state
            self.global_step_number += 1
        self.episode_number += 1

    def sample_experiences(self):
        """sample experiences"""
        return self.memory.sample()

    def pick_action(self, state=None):
        """Picks an action using the actor network and then adds some noise to it to ensure exploration"""
        if state is None:
            state = ms.Tensor(self.state, dtype=ms.float32).unsqueeze(0)
        self.actor_local.set_train(mode=False)

        action = self.actor_local(state).numpy()
        self.actor_local.set_train(mode=True)
        action = self.exploration_strategy.perturb_action_for_exploration_purposes({"action": action})
        return action.squeeze(0)

    def critic_learn(self, states, actions, rewards, next_states, dones):
        """Runs a learning iteration for the critic"""
        critic_targets = self.compute_critic_targets(next_states, rewards, dones)
        # loss = self.compute_loss(states, actions, critic_targets)
        _, grads = self.critic_grad_fn(states, actions, critic_targets)

        self.take_optimisation_step(
            self.critic_optimizer, grads, self.hyperparameters["Critic"]["gradient_clipping_norm"]
        )
        self.soft_update_of_target_network(
            self.critic_local, self.critic_target, self.hyperparameters["Critic"]["tau"]
        )

    def compute_loss(self, states, actions, critic_targets):
        """Computes the loss for the critic"""
        # with torch.no_grad():
        #     critic_targets = self.compute_critic_targets(next_states, rewards, dones)
        critic_expected = self.compute_expected_critic_values(states, actions)
        loss = ops.mse_loss(critic_expected, critic_targets)
        return loss

    def compute_critic_targets(self, next_states, rewards, dones):
        """Computes the critic target values to be used in the loss for the critic"""
        critic_targets_next = self.compute_critic_values_for_next_states(next_states)
        critic_targets = self.compute_critic_values_for_current_states(rewards, critic_targets_next, dones)
        return critic_targets

    def compute_critic_values_for_next_states(self, next_states):
        """Computes the critic values for next states to be used in the loss for the critic"""
        # with torch.no_grad():
        actions_next = self.actor_target(next_states)
        critic_targets_next = self.critic_target(
            ops.cat((next_states, actions_next), axis=1)
        )
        return critic_targets_next

    def compute_critic_values_for_current_states(self, rewards, critic_targets_next, dones):
        """Computes the critic values for current states to be used in the loss for the critic"""
        critic_targets_current = rewards + (self.hyperparameters["discount_rate"] * critic_targets_next * (1.0 - dones))
        return critic_targets_current

    def compute_expected_critic_values(self, states, actions):
        """Computes the expected critic values to be used in the loss for the critic"""
        critic_expected = self.critic_local(
            ops.cat((states, actions), axis=1)
        )
        return critic_expected

    def time_for_critic_and_actor_to_learn(self):
        """Returns boolean indicating whether there are enough experiences to learn from and it is time to learn for the
        actor and critic"""
        return self.enough_experiences_to_learn_from() and self.global_step_number % self.hyperparameters[
            "update_every_n_steps"] == 0

    def actor_learn(self, states):
        """Runs a learning iteration for the actor"""
        if self.done:  # we only update the learning rate at end of each episode
            self.update_learning_rate(self.hyperparameters["Actor"]["learning_rate"], self.actor_optimizer)
        # actor_loss = self.calculate_actor_loss(states)
        _, grads = self.actor_grad_fn(states)
        self.take_optimisation_step(
            self.actor_optimizer, grads, self.hyperparameters["Actor"]["gradient_clipping_norm"]
        )
        self.soft_update_of_target_network(self.actor_local, self.actor_target, self.hyperparameters["Actor"]["tau"])

    def calculate_actor_loss(self, states):
        """Calculates the loss for the actor"""
        actions_pred = self.actor_local(states)
        actor_loss = -self.critic_local(
            ops.cat((states, actions_pred), axis=1)
        ).mean()
        return actor_loss