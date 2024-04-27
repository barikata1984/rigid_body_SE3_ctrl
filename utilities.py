from collections.abc import Iterable
from typing import Optional

import numpy as np
from mujoco._enums import mjtObj
from mujoco._functions import mj_name2id
from mujoco._structs import MjData, MjModel


class Sensors:
    def __init__(self,
                 m: MjModel,
                 d: MjData,
                 ) -> None:
        self.m = m
        self._sensordata = d.sensordata

    def get(self,
            key,
            ):

        idx = get_sensor_measurement_idx(self.m, key)
        return self._sensordata[idx]


def classify_dict_kargs(dict_kargs):
    arr_like = {}
    others = {}

    for k, v in dict_kargs.items():
        if isinstance(v, str):
            others[k] = v
        elif isinstance(v, Iterable):
            arr_like[k] = v
        else:
            others[k] = v

    return arr_like, others


def get_element_id(m: MjModel,
                   elem_type: str,
                   name: str,
                   ) -> int:
    obj_enum = None

    if "body"== elem_type:
        obj_enum = mjtObj.mjOBJ_BODY
    elif "camera"== elem_type:
        obj_enum = mjtObj.mjOBJ_CAMERA
    elif "joint"== elem_type:
        obj_enum = mjtObj.mjOBJ_JOINT
    elif "sensor"== elem_type:
        obj_enum = mjtObj.mjOBJ_SENSOR
    elif "site"== elem_type:
        obj_enum = mjtObj.mjOBJ_SITE
    else:
        raise ValueError(f"'{elem_type}' is not supported for now. Use mj_name2id and check the value of an ID instead.")

    id = mj_name2id(m, obj_enum, name)

    if -1 == id:
        raise ValueError(f"ID for '{name}' not found. Check the manipulator .xml or the object .xml")

    return id

def get_sensor_measurement_idx(m: MjModel,
                               name: Optional[str] = None,
                               id: Optional[int] = None,
                               ) -> list[int]:


    if id is None:
        if name is None:
            raise ValueError("'name' have to be set when 'id' is None")
        id = get_element_id(m, "sensor", name)

    idx = np.arange(m.sensor_dim[id]) + m.sensor_dim[:id].sum()
    return idx.tolist()
