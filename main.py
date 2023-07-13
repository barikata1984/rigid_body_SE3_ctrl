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
from datetime import datetime
from scipy import linalg
from tqdm import tqdm


# Remove redundant space at the head and tail of the horizontal axis's scale
mpl.rcParams['axes.xmargin'] = 0
# Reduce the number of digits of values with numpy
np.set_printoptions(precision=3, suppress=True)


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
    # Compose the principal spatial inertia matrix for each link including the
    # worldbody
    body_spati_i = np.array([
        dyn.compose_spati_i(m, i) for m, i in zip(m.body_mass, m.body_inertia)])
    # Convert sinert_i to sinert_b rel2 the body frame
    body_spati_pose_b = tf.posquat2SE3(m.body_ipos, m.body_iquat)
    body_spati_b = dyn.transfer_sinert(body_spati_pose_b, body_spati_i)

    # Configure SE3 of child frame rel2 parent frame (M_{i, i - 1} in MR)
    pose_home_ba = tf.posquat2SE3(m.body_pos, m.body_quat)
    # Configure SE3 of each body frame rel2 worldbody (M_{i} = M_{0, i} in MR)
    pose_home_xb = [pose_home_ba[0].inv()]  # xb = 00, 01, ..., 06
    for p_h_ba in pose_home_ba[1:]:
        pose_home_xb.append(pose_home_xb[-1].dot(p_h_ba.inv()))

    # Obtain unit screw rel2 each link = body (A_{i} in MR)
    uscrew_bb = np.zeros((m.body_jntnum.sum(), 6))  # bb = (11, 22, ..., 66)
    for b, (type, ax) in enumerate(zip(m.jnt_type, m.jnt_axis), 0):
        slicer = 3 * (type - 2)  # type is 2 for slide and 3 for hinge
        uscrew_bb[b, slicer:slicer + 3] = ax / linalg.norm(ax)

    # Set (d)twist vectors for the worldbody to be used for inverse dynamics
    twist_00 = np.zeros(6)
    gacc_x = np.zeros(6)
    gacc_x[:3] = -mj.MjOption().gravity
    dtwist_00 = gacc_x.copy()

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
    tgt_ctrl = []
    # Dictionary to be converted to a .json file for training
    transforms = dict(
        date_time=datetime.now().strftime("%d/%m/%Y_%H:%M:%S"),
        camera_angle_x=cam.fovx,
        frames=list(),
    )
    # Make a directory in which the .json file is saved
    dataset_hierarchy = ["data", "images"]
    obs_dir = os.path.join(*dataset_hierarchy)
    if not os.path.isdir(obs_dir):
        os.makedirs(obs_dir)
    # Others
    sensordata = []
    time = []
    frame_count = 0

    # =========================================================================
    # Main loop
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    for step in tqdm(range(t.n_steps)):
        traj.append(plan(step))
        tgt_ctrl.append(
            dyn.inverse(
                traj[-1], pose_home_ba, body_spati_b, uscrew_bb, twist_00,
                dtwist_00
                )
            )

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
        ctrl.append(tgt_ctrl[-1] - K @ res_state)
        d.ctrl = ctrl[-1]

        # Evolute the simulation
        mj.mj_step(m, d)

        # Store other necessary data
        sensordata.append(d.sensordata.copy())
        time.append(d.time)

        # Store frames following the fps
        if frame_count <= time[-1] * t.fps:
            # Write a video ===================================================
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

            # Do some SE(3) math ==============================================
            # Camera pose rel. to the object
            obj_pose_x = tf.trzs2SE3(d.xpos[obj_id], d.xmat[obj_id])
            cam_pose_x = tf.trzs2SE3(d.cam_xpos[cam.id], d.cam_xmat[cam.id])
            cam_pose_obj = obj_pose_x.inv().dot(cam_pose_x)
            # FT sensor pose rel. to the object
            sen_pose_x = tf.trzs2SE3(d.site_xpos[sen_id], d.site_xmat[sen_id])
            sen_pose_obj = obj_pose_x.inv().dot(sen_pose_x)
            # Object acceleration rel. to the sensor
            obj_acc_x = sensordata[-1][4 * m.nu:] - gacc_x
            obj_acc_sen = sen_pose_x.inv().adjoint() @ obj_acc_x
            # Sensor measurement
            ft = sensordata[-1][4 * m.nu:].tolist()

            # Log NeMD ingredients ============================================
            frame = dict(
                file_path=os.path.join(dataset_hierarchy[1], file_name),
                cam_pose_obj=cam_pose_obj.as_matrix().tolist(),
                obj_pose_sen=sen_pose_obj.inv().as_matrix().tolist(),
                obj_acc_sen=obj_acc_sen.tolist(),
                ft=ft
                )

            transforms["frames"].append(frame)

            # Sampling for NeMD terminated while "frame_count" incremented
            frame_count += 1

    # VideoWriter released
    out.release()

    _, _, sens_qfrc, sens_ft, obj_acc_x = np.split(
        sensordata, [1 * m.nu, 2 * m.nu, 3 * m.nu, 4 * m.nu], axis=1)

    with open("./data/nemd_multiview.json", "w") as f:
        json.dump(transforms, f, indent=2)

    # Convert lists of logged data into ndarrays ==============================
    traj = np.asarray(traj)
    qpos = np.asarray(qpos)
    tgt_ctrl = np.asarray(tgt_ctrl)

    # Visualize data ==========================================================
    # Actual and target joint positions
    qpos_fig, qpos_axes = plt.subplots(2, 1, sharex="col", tight_layout=True)
    qpos_fig.suptitle("qpos")
    qpos_axes[1].set(xlabel="time [s]")
    yls = ["q0-2 [m]", "q3-5 [rad]"]
    for i in range(len(qpos_axes)):
        slcr = slice(i * 3, (i + 1) * 3)
        vis.ax_plot_lines_w_tgt(
            qpos_axes[i], time, qpos[:, slcr], traj[:, 0, slcr], yls[i])

    # Joint forces and torques
    ctrl_fig, ctrl_axes = plt.subplots(3, 1, sharex="col", tight_layout=True)
    ctrl_fig.suptitle("act_qfrc VS tgt_ctrl")
    ctrl_axes[0].set(ylabel="q0-1 [N]")
    ctrl_axes[1].set(ylabel="q2 [N]")
    ctrl_axes[2].set(xlabel="time [s]")
    vis.axes_plot_frc(
        ctrl_axes[:2], time, sens_qfrc[:, 0:3], tgt_ctrl[:, 0:3])
    vis.ax_plot_lines_w_tgt(
        ctrl_axes[2], time, sens_qfrc[:, 3:6], tgt_ctrl[:, 3:6], "q3-5 [N·m]")

    # FT sensor mesurements
    ft_fig, ft_axes = plt.subplots(2, 1, sharex="col", tight_layout=True)
    ft_fig.suptitle("ft")
    yls = ["x/y/z-frc of {s} [N]", "x/y/z-trq of {s} [N·m]"]
    for i in range(len(ft_axes)):
        slcr = slice(i * 3, (i + 1) * 3)
        vis.ax_plot_lines(ft_axes[i], time, sens_ft[:, slcr], yls[i])
    
    # Object acceleration rel. to {world}
    obj_acc_x_fig, obj_acc_x_axes = plt.subplots(2, 1, tight_layout=True)
    obj_acc_x_fig.suptitle("obj_acc_x")
    yls = ["obj_linacc_x", "obj_angacc_x"]
    for i in range(len(obj_acc_x_axes)):
        slcr = slice(i * 3, (i + 1) * 3)
        vis.ax_plot_lines(obj_acc_x_axes[i], time, obj_acc_x[:, slcr], yls[i])

    plt.show()


if __name__ == "__main__":
    main()

