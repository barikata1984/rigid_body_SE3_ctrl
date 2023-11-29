import os
import cv2
import json
import numpy as np
import mujoco as mj
import dynamics as dyn
import matplotlib as mpl
import visualization as vis
import transformations as tf
from matplotlib import pyplot as plt
from configure import load_configs
from liegroups import SO3, SE3
from datetime import datetime
from scipy import linalg
from tqdm import tqdm


# Remove redundant space at the head and tail of the horizontal axis's scale
mpl.rcParams['axes.xmargin'] = 0
# Reduce the number of digits of values with numpy
np.set_printoptions(precision=6, suppress=True)


def main():
    config_file = "./configs/config.toml"
    m, d, t, cam, ss, plan = load_configs(config_file)

    K = dyn.compute_gain_matrix(m, d, ss)

    out = cv2.VideoWriter(
        cam.output_file, cam.fourcc, t.fps, (cam.width, cam.height))
    renderer = mj.Renderer(
        m, cam.height, cam.width)

    # Description of suffixes used from the section below:
    #   This   |        |
    #  project | MuJoCo | Description
    # ---------+--------+------------
    #    _x    |  _x    | Described in {cartesian} or {world}
    #    _b    |  _b    | Descried in {body)
    #    _i    |  _i    | Described in {principal} of each body
    #    _q    |  _q    | Described in the joint space
    #    _xi   |  _xi   | Of {principal} rel. to {world}
    #    _ab   |   -    | Of {body} rel. to {parent}
    #    _ba   |   -    | Of {parent} rel. to {body}
    #    _m    |   -    | Described in the frame a joint corresponds to
    #
    # Compose the principal spatial inertia matrix for each body including the
    # worldbody

    body_spati_i = np.array([
        dyn.compose_spati_i(m, i) for m, i in zip(m.body_mass, m.body_inertia)])
    # Convert sinert_i to sinert_b rel2 the body frame
    body_spati_pose_b = tf.posquat2SE3(m.body_ipos, m.body_iquat)
    body_spati_b = dyn.transfer_sinert(body_spati_pose_b, body_spati_i)

    mom_i = np.array([
        *body_spati_b[-1, 3:, 3:].diagonal(),
        *body_spati_b[-1, 3, 4:],
        body_spati_b[-1, 4, 5]])

    print("Target object's inertial parameters wr2 its body frame ======\n"
         f"    Mass:               {m.body_mass[-1]}\n"
         f"    First moments:      {SO3.vee(body_spati_b[-1, 3:, :3])}\n"
         f"    Moments of inertia: {mom_i}\n")

    # Configure SE3 of child frame rel2 parent frame (M_{i, i - 1} in MR)
    pose_home_ba = tf.posquat2SE3(m.body_pos, m.body_quat)
    # Configure SE3 of each body frame rel2 worldbody (M_{i} = M_{0, i} in MR)
    pose_home_xb = [pose_home_ba[0].inv()]  # xb = 00, 01, ..., 06
    for p_h_ba in pose_home_ba[1:]:
        pose_home_xb.append(pose_home_xb[-1].dot(p_h_ba.inv()))

    # Obtain unit screw rel2 each link = body (A_{i} in MR)
    uscrew_bb = np.zeros((m.body_jntnum.sum(), 6))  # bb = (11, 22, ..., 66)
    for b, (jnt_type, ax) in enumerate(zip(m.jnt_type, m.jnt_axis), 0):
        slicer = 3 * (jnt_type - 2)  # jnt_type: 2 for slide, 3 for hinge
        uscrew_bb[b, slicer:slicer + 3] = ax / linalg.norm(ax)

    # Set up dynamics related variables =======================================
    # (d)twist vectors for the worldbody to be used for inverse dynamics
    twist_00 = np.zeros(6)
    gacc_x = np.zeros(6)
    gacc_x[:3] = -mj.MjOption().gravity
    dtwist_00 = gacc_x.copy()
    # gain matrix for linear quadratic regulator
    K = dyn.compute_gain_matrix(m, d, ss)

    # Prepare for data logging ================================================
    # IDs for convenience
    sen_id = mj.mj_name2id(m, mj.mjtObj.mjOBJ_SITE, "ft_sen")
    obj_id = mj.mj_name2id(m, mj.mjtObj.mjOBJ_BODY, "object")
    # Trajectory
    traj = []
    # Joint positions
    qpos = []
    # Residual of qpos
    res_qpos = np.empty(m.nu)
    # Control signals
    ctrl = []
    tgt_ctrls = []
    # Scale sampled coordinates ∈ (-1, 1) in wisp to the dimensions of an axis-
    # aligned bounding box of the object.
    # NOTE: setting the longest edge of the bounding box is recommended because
    # choosing a cuboid rather than a cube may affect the prediction result of
    # NeMD, the auther haven't checked it yet tho
#    aabb_scale = 0.5
    # Dictionary to be converted to a .json file for training
    transforms = dict(
        date_time=datetime.now().strftime("%d/%m/%Y_%H:%M:%S"),
        camera_angle_x=cam.fovx,
#        aabb_scale=aabb_scale,
        frames=list(),
    )

    # Make a directory in which the .json file is saved
    dataset_dir = "data/uniform"
#    dataset_dir = "data/composite"
    dataset_hierarchy = [dataset_dir, "images"]
    obs_dir = os.path.join(*dataset_hierarchy)
    if not os.path.isdir(obs_dir):
        os.makedirs(obs_dir, exist_ok=True)

    # Others
    sensorreads = []
    time = []
    frame_count = 0
    obj_ttl_linaccs_sen = []
    obj_twists_sen = []
    obj_accels_sen = []
    fts_sen = []

    # =========================================================================
    # Main loop
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    for step in tqdm(range(t.n_steps), desc="Progress of simulation"):
        traj.append(plan(step))

        tgt_ctrl, parent_poses_child, twists, dtwists = dyn.inverse(
            traj[-1], pose_home_ba, body_spati_b, uscrew_bb, twist_00, dtwist_00)
        tgt_ctrls.append(tgt_ctrl)

        # Retrieve joint variables
        qpos.append(d.qpos.copy())
        # Residual of state
        mj.mj_differentiatePos(  # Use this func to differenciate quat properly
            m,  # MjModel
            res_qpos,  # data buffer for the residual of qpos
            m.nu,  # idx of a joint up to which res_qpos are calculated
            qpos[-1],  # current qpos
            traj[-1][0])  # target qpos or next qpos to calkculate dqvel
        res_state = np.concatenate((res_qpos, traj[-1][1] - d.qvel))
        # Compute and set control, or actuator inputs
        ctrl.append(tgt_ctrls[-1] - K @ res_state)
        d.ctrl = ctrl[-1]

        # Evolute the simulation
        mj.mj_step(m, d)

        # Store other necessary data
        sensorread = d.sensordata.copy()
        sensorreads.append(sensorread)
        time.append(d.time)

        aabb_scale = 0.3

        # Camera pose rel. to the object
        obj_pose_x = tf.trzs2SE3(d.xpos[obj_id], d.xmat[obj_id])
        cam_pose_x = tf.trzs2SE3(d.cam_xpos[cam.id], d.cam_xmat[cam.id])
        cam_pose_obj = obj_pose_x.inv().dot(cam_pose_x)
        # FT sensor pose rel. to the object
        sen_pose_x = tf.trzs2SE3(d.site_xpos[sen_id], d.site_xmat[sen_id])
        x_pose_sen = sen_pose_x.inv()  # SE3Matrix object
        x_rot_sen = x_pose_sen.rot  # SO3Matrix object
        sen_pose_obj = obj_pose_x.inv().dot(sen_pose_x)
        obj_pose_sen = sen_pose_obj.inv()

        obj_vel_x = sensorread[4*m.nu:5*m.nu]
        obj_twist_sen = x_pose_sen.adjoint() @ obj_vel_x

        obj_acc_x = sensorread[5*m.nu:6*m.nu]
        obj_linacc_x = obj_acc_x[:3]
        obj_ttl_linacc_sen = x_rot_sen.dot(obj_linacc_x)
        obj_linacc_sen = obj_ttl_linacc_sen - np.cross(obj_twist_sen[:3], obj_twist_sen[3:])

        obj_angacc_sen = x_rot_sen.dot(obj_acc_x[3:])
        obj_accel_sen = [obj_linacc_sen.tolist(), obj_angacc_sen.tolist()]

        ft_sen = sensorread[3*m.nu:4*m.nu].tolist()  # Store frames following the fps

        # ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ 検証用コード追加ゾーン ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ 
        # obj_linvel/acc_x の復元 =================================================
        _obj_twist_obj = twists[-1]
        _obj_linvel_x = dyn.compute_linvel(obj_pose_x, _obj_twist_obj, coord_xfer_twist=True)
                                            # framelinvel と絶対誤差 0.0016 くらい。mujoco も c++ で
                                            # 書いたラグランジュの運動方程式で模擬してるわけで、
                                            # それを真値あつかいしてる分の誤差も乗っていそう

        _obj_dtwist_obj = dtwists[-1]
        _obj_dtwist_x = dyn.coordinate_transform_dtwist(
            obj_pose_x, _obj_twist_obj, _obj_dtwist_obj, coord_xfer_twist=True)
        _obj_linacc_x = dyn.compute_linacc(
            obj_pose_x, _obj_twist_obj, _obj_dtwist_x, coord_xfer_twist=True)

        # obj_linacc_sen の復元 ===============================================
        _obj_twist_obj = twists[-1]
        _obj_dtwist_sen = dyn.coordinate_transform_dtwist(
            obj_pose_sen, _obj_twist_obj, _obj_dtwist_obj, coord_xfer_twist=True)
        _obj_linacc_sen = dyn.compute_linacc(
            obj_pose_sen, _obj_twist_obj, _obj_dtwist_sen, coord_xfer_twist=True)


        if frame_count <= time[-1] * t.fps:
#            tol = 1e-03
#            print(f"{np.isclose(__obj_linacc_x, _obj_linacc_x)=}")
#            print(f"{abs(__obj_linacc_x - _obj_linacc_x)=}")

#            print("==========================================================")
            print(f"{         ft_sen[0]=}")
            print(f"{_obj_linacc_sen[0]=}")
            total_mass = ft_sen[:3] / _obj_linacc_sen
            print(f"{     total_mass[0]=}")  # x 軸の計算が甘いが median を取れば行けた
                                             # 積み上げ誤差がまぁまぁあるけど一旦そこは無視
                                             # x 軸のみ near-zero division とかになってそう

            # ↑ ↑ ↑ ↑ ↑ ↑ ↑ ↑ ↑ ↑ ↑ ↑ 検証用コード追加ゾーン ↑ ↑ ↑ ↑ ↑ ↑ ↑ ↑ ↑ ↑ ↑ ↑

            # Writing a single frame of a dataset =============================
            renderer.update_scene(d, cam.id)
            bgr = renderer.render()[:, :, [2, 1, 0]]
            # Make an alpha mask to remove the black background
            alpha = np.where(
                np.all(bgr == 0, axis=-1), 0, 255)[..., np.newaxis]
            file_name = f"{frame_count:04}"
            cv2.imwrite(
                os.path.join(*dataset_hierarchy, file_name) + ".png",
                np.append(bgr, alpha, axis=2))  # image (bgr + alpha)
            # Write a video frame
            out.write(bgr)

            # Log velocity components relative to the sensor frame
            obj_twists_sen.append(obj_twist_sen)
            # Log acceleration components relative to the sensor frame
            obj_ttl_linaccs_sen.append(obj_ttl_linacc_sen)
            obj_accels_sen.append(obj_accel_sen)
            # Log force and torque mesurements 
            fts_sen.append(ft_sen)

            # Log NeMD ingredients ============================================
            frame = dict(
                file_path=os.path.join(dataset_hierarchy[1], file_name),
                cam_pose_obj=cam_pose_obj.as_matrix().tolist(),
                obj_pose_sen=obj_pose_sen.as_matrix().tolist(),
                obj_twist_sen=obj_twist_sen.tolist(),
                obj_accel_sen=obj_accel_sen,
#                obj_linacc_sen=obj_linacc_sen,
                aabb_scale=[aabb_scale],
                ft_sen=ft_sen,
                )

            transforms["frames"].append(frame)

            # Sampling for NeMD terminated while "frame_count" incremented
            frame_count += 1

    # VideoWriter released
    out.release()

#    qpos_meas, qvel_meas, qfrc_meas, ft_meas_sen, obj_vel_x, obj_acc_x = np.split(
#        sensordata, [1*m.nu, 2*m.nu, 3*m.nu, 4*m.nu, 5*m.nu], axis=1)

    with open(f"./{dataset_dir}/transform.json", "w") as f:
        json.dump(transforms, f, indent=2)

    # Convert lists of logged data into ndarrays ==============================
    traj = np.asarray(traj)
    qpos = np.asarray(qpos)
    tgt_ctrls = np.asarray(tgt_ctrls)

    # Visualize data ==========================================================
    # Object linear acceleration and ft sensor measurements rel. to {sensor}
    # Actual and target joint positions
    qpos_fig, qpos_axes = plt.subplots(2, 1, sharex="col", tight_layout=True)
    qpos_fig.suptitle("qpos")
    qpos_axes[1].set(xlabel="time [s]")
    yls = ["q0-2 [m]", "q3-5 [rad]"]
    for i in range(len(qpos_axes)):
        slcr = slice(i * 3, (i + 1) * 3)
        vis.ax_plot_lines_w_tgt(
            qpos_axes[i], time, qpos[:, slcr], traj[:, 0, slcr], yls[i])


    print(f"{qpos.shape=}")
    print(f"{traj.shape=}")


    # Object linear acceleration and ft sensor measurements rel. to {sensor}
    fts_sen = np.array(fts_sen)
    acc_ft_fig, acc_ft_axes = plt.subplots(3, 1, tight_layout=True)
    acc_ft_fig.suptitle("linacc vs ft")
    acc_ft_axes[0].set(xlabel="# of frames")
    acc_ft_axes[2].set(xlabel="time [s]")
    vis.ax_plot_lines(
        acc_ft_axes[0], range(len(obj_ttl_linaccs_sen)), np.array(obj_ttl_linaccs_sen),
        "obj_linacc_sen [m/s/s]"
        )
    vis.ax_plot_lines(
        acc_ft_axes[1], time, fts_sen[:, :3], "frc_sen [N]")
    vis.ax_plot_lines(
        acc_ft_axes[2], time, fts_sen[:, 3:], "trq_sen [N·m]")

    # Joint forces and torques
#     ctrl_fig, ctrl_axes = plt.subplots(3, 1, sharex="col", tight_layout=True)
#     ctrl_fig.suptitle("act_qfrc VS tgt_ctrls")
#     ctrl_axes[0].set(ylabel="q0-1 [N]")
#     ctrl_axes[1].set(ylabel="q2 [N]")
#     ctrl_axes[2].set(xlabel="time [s]")
#     vis.axes_plot_frc(
#         ctrl_axes[:2], time, sens_qfrc[:, :3], tgt_ctrls[:, :3])
#     vis.ax_plot_lines_w_tgt(
#         ctrl_axes[2], time, sens_qfrc[:, 3:], tgt_ctrls[:, 3:], "q3-5 [N·m]")

    plt.show()


if __name__ == "__main__":
    main()
