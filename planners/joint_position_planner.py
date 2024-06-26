import re
from collections.abc import Callable
from dataclasses import dataclass, field
from math import pi
from typing import Any, Union

import numpy as np
from mujoco._structs import MjData, MjModel, MjOption
from numpy import linalg as la
from numpy.typing import ArrayLike, NDArray
from omegaconf import MISSING
from omegaconf.errors import MissingMandatoryValue


@dataclass
class JointPositionPlannerConfig:
    target_class: str = "JointPositionPlanner"
    duration: float = MISSING
    timestep: float = -1
    pos_offset: list[float] = MISSING
    displacements: list[Union [float, str]] = field(default_factory=lambda: [0.2, 0.4, 0.6,
                                                                             1.0 * pi,
                                                                             0.3 * pi,
                                                                             1.5 * pi])

class JointPositionPlanner:
    def __init__(self,
                 cfg: JointPositionPlannerConfig,
                 m: MjModel,
                 d: MjData,
                 ) -> None:

        # Fill a potentially missing field of a planner configuration
        try:
            cfg.pos_offset
        except MissingMandatoryValue:
            cfg.pos_offset = d.qpos.copy().tolist()  # awkward but omegaconf
                                                     # does not support NDArray

        self.duration = cfg.duration
        self.timestep = MjOption().timestep if cfg.timestep <= 0 else cfg.timestep
        self.n_steps = int(self.duration / self.timestep)

        displacements = []
        for _disp in cfg.displacements:
            disp = _disp.__repr__().strip("'")  # not sure this is the best solution...
            try:
                disp = float(disp)
            except ValueError:
                disp = self.safe_eval(self.replace_pi(disp))

            displacements.append(disp)

        self.displacements = displacements
        self.plan = traj_5th_spline(self.displacements, cfg.pos_offset,
                                    self.timestep, self.n_steps)

        #print("Simulation time setup =======================================\n"
        #     f"    Number of steps:            {self.n_steps}\n"
        #     f"    Simulation time [s]:        {self.duration}\n"
        #     f"    Timestep [s]:               {self.timestep}\n"
        #     f"    Simulation freq. [Hz]:      {1 / self.timestep}\n"
        #     f"    Rendering freq. [Hz]:       {self.fps}")

        #print("Simulation time setup =======================================\n"
        #     f"    qpos: {d.qpos}\n"
        #      "            ↓\n"
        #     f"          {d.qpos + dqpos}")

        #return pln.traj_5th_spline(start_qpos, goal_qpos, t.timestep, t.n_steps)


    def safe_eval(self, expr):
      """Evaluates a mathematical expression if it contains only allowed characters."""
      allowed_chars = "0123456789.+*/-() "
      if all(c in allowed_chars for c in expr):
        return eval(expr)
      else:
        raise ValueError("Invalid characters in expression.")

    def replace_pi(self, text):
      """Replaces occurrences of "pi" with its numerical value in a string."""
      return re.sub(r"\bpi\b", str(pi), text)


def traj_5th_spline(displacement: ArrayLike,
                    pos_offset: ArrayLike,
                    timestep: float,
                    n_steps: int,
                    init_step: int = 0,
                    ) -> Callable[[int], NDArray]:

    # Set the time window
    t_s = init_step
    t_e = t_s + n_steps

    # Define normalized boundaries of a spline
    normd_bounds = np.array([
        0, 1,  # start/end pos
        0, 0,  # start/end vel 0 to make the motion smoooth
        0, 0])  # set start/end acc at 0 ↑

    # define the parameter matrix for a differentiated fifth-order polynomial
    spline_matrix = np.array([
        [t_s**5, t_s**4, t_s**3, t_s**2, t_s, 1],
        [t_e**5, t_e**4, t_e**3, t_e**2, t_e, 1],
        [5 * t_s**4, 4 * t_s**3, 3 * t_s**2, 2 * t_s**1, 1, 0],
        [5 * t_e**4, 4 * t_e**3, 3 * t_e**2, 2 * t_e**1, 1, 0],
        [20 * t_s**3, 12 * t_s**2, 6 * t_s**1, 2, 0, 0],
        [20 * t_e**3, 12 * t_e**2, 6 * t_e**1, 2, 0, 0]],
        dtype=float)

    # compute the constants of the fifth-order spline
    coeffs = la.solve(spline_matrix, normd_bounds).squeeze()

    displacement = np.array(displacement)
    pos_offset = np.array(pos_offset)

    def plan(step: int):
        fifth = np.array([step**i for i in range(5, -1, -1)])
        fourth = np.array([step**i * (i + 1) for i in range(4, -1, -1)])
        third = np.array([
            step**i * (i + 1) * (i + 2)for i in range(3, -1, -1)])

        pos = displacement * np.dot(coeffs[:], fifth) + pos_offset
        vel = displacement * np.dot(coeffs[:-1], fourth) / timestep
        acc = displacement * np.dot(coeffs[:-2], third) / timestep**2

        return np.array([pos, vel, acc])

    return plan
