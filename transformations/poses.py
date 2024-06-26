from liegroups import SE3
from mujoco._structs import MjData, MjModel

from utilities import get_element_id
from .transformations import compose


class Poses:
    def __init__(self,
                m: MjModel,
                d: MjData,
                ) -> None:
        self.m = m
        self.a_b = compose(m.body_pos, m.body_quat)
        self.b_bi = compose(m.body_ipos, m.body_iquat)
        self.x_b = compose(d.xpos, d.xmat)
        self.x_bi = compose(d.xipos, d.ximat)
        self.x_cam = compose(d.cam_xpos, d.cam_xmat)
        self.x_site = compose(d.site_xpos, d.site_xmat)
        self.l_lj = [SE3.identity()] + compose(m.jnt_pos)  # x~last = x + first~last
        self.lj_li = []
        for l_lj, l_li in zip(self.l_lj, self.b_bi):  # num iter == len(self.l_lj)
            self.lj_li.append(l_lj.inv().dot(l_li))

    def get_a_(self,
               name,
               ) -> SE3:
        return self.a_b[get_element_id(self.m, "body", name)]

    def get_b_biof(self,
                 name,
                 ) -> SE3:
        return self.b_bi[get_element_id(self.m, "body", name)]

    def get_x_(self,
               elem_type,
               name,
               ) -> SE3:

        if "body" == elem_type:
            return self.x_b[get_element_id(self.m, elem_type, name)]
        elif "pricipal" == elem_type:
            return self.x_bi[get_element_id(self.m, "body", name)]
        elif "camera" == elem_type:
            return self.x_cam[get_element_id(self.m, elem_type, name)]
        elif "site" == elem_type:
            return self.x_site[get_element_id(self.m, elem_type, name)]
        else:
            raise ValueError(f"Pose retrievazl fo element type {elem_type} is not supported for now")
