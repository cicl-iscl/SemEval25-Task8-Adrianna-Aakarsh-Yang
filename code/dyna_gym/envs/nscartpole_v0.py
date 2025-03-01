"""
NSCartPole-v0

Cart-pole system with a dynamic transition function
"""

import logging
import math
import gymnasium as gym
from gymnasium import error, spaces, utils
from gymnasium.utils import seeding
import numpy as np

logger = logging.getLogger(__name__)

class NSCartPoleV0(gym.Env):
    metadata = {
        'render.modes': ['human', 'rgb_array'],
        'video.frames_per_second' : 50
    }

    def __init__(self):
        self.gravity = 9.8
        self.masscart = 1.0
        self.masspole = 0.1
        self.total_mass = (self.masspole + self.masscart)
        self.length = 0.5 # actually half the pole's length
        self.polemass_length = (self.masspole * self.length)
        self.force_mag = 10.0
        self.nb_actions = 5 # number of discrete actions in [-force_mag,+force_mag]
        self.tau = 0.02  # seconds between state updates

        # Angle at which to fail the episode
        self.theta_threshold_radians = 12 * 2 * math.pi / 360
        self.x_threshold = 2.4

        # Dynamic parameters
        self.alpha_max_radians = 20 * 2 * math.pi / 360 # maximum inclination
        self.alpha_period = 2. # inclination period in seconds

        # Angle limit set to 2 * theta_threshold_radians so failing observation is still within bounds
        high = np.array([
            self.x_threshold * 2,
            np.finfo(np.float32).max,
            self.theta_threshold_radians * 2,
            np.finfo(np.float32).max])

        self.action_space = spaces.Discrete(self.nb_actions)
        self.observation_space = spaces.Box(-high, high)

        self._seed()
        self.viewer = None
        self.state = None
        self.steps_beyond_done = None

    def equality_operator(self, s1, s2):
        '''
        Equality operator, return True if the two input states are equal.
        Only test the 4 first components (x, x_dot, theta, theta_dot)
        '''
        for i in range(4):
            if not math.isclose(s1[i], s2[i], rel_tol=1e-5):
                return False
        return True

    def reset(self):
        self.state = self.np_random.uniform(low=-0.05, high=0.05, size=(4,))
        self.state = np.append(self.state,0.0) # time
        self.steps_beyond_done = None
        return np.array(self.state)

    def _seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def transition(self, state, action, is_model_dynamic):
        '''
        Transition operator, return the resulting state, reward and a boolean indicating
        whether the termination criterion is reached or not.
        The boolean is_model_dynamic indicates whether the temporal transition is applied
        to the state vector or not (increment of tau).
        '''
        x, x_dot, theta, theta_dot, time = state

        self.alpha = self.alpha_max_radians * math.sin(time * 6.28318530718 / self.alpha_period)
        cosalpha = math.cos(self.alpha)
        force = - self.force_mag + action * 2 * self.force_mag / (self.nb_actions - 1)
        force = force * cosalpha - self.gravity * math.sin(self.alpha)
        gravity = self.gravity * cosalpha

        costheta = math.cos(theta)
        sintheta = math.sin(theta)
        temp = (force + self.polemass_length * theta_dot * theta_dot * sintheta) / self.total_mass
        thetaacc = (gravity * sintheta - costheta* temp) / (self.length * (4.0/3.0 - self.masspole * costheta * costheta / self.total_mass))
        xacc  = temp - self.polemass_length * thetaacc * costheta / self.total_mass
        x  = x + self.tau * x_dot
        x_dot = x_dot + self.tau * xacc
        theta = theta + self.tau * theta_dot
        theta_dot = theta_dot + self.tau * thetaacc
        if is_model_dynamic:
            time = time + self.tau
        state_p = (x,x_dot,theta,theta_dot,time)
        done =  x < -self.x_threshold \
                or x > self.x_threshold \
                or theta < -self.theta_threshold_radians \
                or theta > self.theta_threshold_radians
        done = bool(done)

        if not done:
            reward = 1.0
        else:
            reward = 0.0
        '''
        elif self.steps_beyond_done is None:
            # Pole just fell!
            self.steps_beyond_done = 0
            reward = 1.0
        else:
            if self.steps_beyond_done == 0:
                logger.warning("You are calling 'step()' even though this environment has \
                already returned done = True. You should always call 'reset()' once you \
                receive 'done = True' -- any further steps are undefined behavior.")
            self.steps_beyond_done += 1
            reward = 0.0
        '''
        return state_p, reward, done

    def step(self, action):
        '''
        Step function equivalent to transition and reward function.
        Actually modifies the environment's state attribute.
        Return (observation, reward, termination criterion (boolean), informations)
        '''
        assert self.action_space.contains(action), "%r (%s) invalid"%(action, type(action))
        self.state, reward, done = self.transition(self.state, action, True)
        return np.array(self.state), reward, done, {}

    def print_state(self):
        print('x: {:.5f}; x_dot: {:.5f}; theta: {:.5f}; theta_dot: {:.5f}'.format(self.state[0],self.state[1],self.state[2],self.state[3]))

    def render(self, mode='human', close=False):
        if close:
            if self.viewer is not None:
                self.viewer.close()
                self.viewer = None
            return

        screen_width = 600
        screen_height = 400

        world_width = self.x_threshold*2
        scale = screen_width/world_width
        carty = 100 # TOP OF CART
        polewidth = 10.0
        polelen = scale * 1.0
        cartwidth = 50.0
        cartheight = 30.0

        if self.viewer is None:
            from gymnasium.envs.classic_control import rendering
            self.viewer = rendering.Viewer(screen_width, screen_height)
            l,r,t,b = -cartwidth/2, cartwidth/2, cartheight/2, -cartheight/2
            axleoffset =cartheight/4.0
            cart = rendering.FilledPolygon([(l,b), (l,t), (r,t), (r,b)])
            self.carttrans = rendering.Transform()
            cart.add_attr(self.carttrans)
            self.viewer.add_geom(cart)

            l,r,t,b = -polewidth/2,polewidth/2,polelen-polewidth/2,-polewidth/2
            pole = rendering.FilledPolygon([(l,b), (l,t), (r,t), (r,b)])
            pole.set_color(.8,.6,.4)
            self.poletrans = rendering.Transform(translation=(0, axleoffset))
            pole.add_attr(self.poletrans)
            pole.add_attr(self.carttrans)
            self.viewer.add_geom(pole)

            l,r,t,b = -1,1,50,-50
            vect = rendering.FilledPolygon([(l,b), (l,t), (r,t), (r,b)])
            vect.set_color(1,0,0)
            self.vecttrans = rendering.Transform(translation=(0.2*screen_width, 0.8*screen_height))
            vect.add_attr(self.vecttrans)
            self.viewer.add_geom(vect)

            self.axle = rendering.make_circle(polewidth/2)
            self.axle.add_attr(self.poletrans)
            self.axle.add_attr(self.carttrans)
            self.axle.set_color(.5,.5,.8)

            self.viewer.add_geom(self.axle)
            self.track = rendering.Line((0,carty), (screen_width,carty))
            self.track.set_color(0,0,0)
            self.viewer.add_geom(self.track)

        if self.state is None: return None

        x = self.state
        cartx = x[0]*scale+screen_width/2.0 # MIDDLE OF CART
        self.carttrans.set_translation(cartx, carty)
        self.poletrans.set_rotation(-x[2])
        self.vecttrans.set_rotation(self.alpha)

        return self.viewer.render(return_rgb_array = mode=='rgb_array')
