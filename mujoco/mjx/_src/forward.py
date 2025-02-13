from typing import Optional

import warp as wp

from . import math
from . import smooth
from . import types


@wp.func
def quat_integrate_wp(q: wp.quat, v: wp.vec3, dt: wp.float32) -> wp.quat:
  """Integrates a quaternion given angular velocity and dt."""
  norm_ = wp.length(v)
  v = wp.normalize(v)  # does that need proper zero gradient handling?
  angle = dt * norm_

  q_res = math.axis_angle_to_quat(v, angle)
  q_res = math.mul_quat(q, q_res)

  return wp.normalize(q_res)


WarpMjMinVal = wp.constant(1e-15)


@wp.kernel
def next_activation(
    m: types.Model, d: types.Data, act_dot_in: wp.array2d(dtype=wp.float32)
):
  worldId, tid = wp.tid()

  # get the high/low range for each actuator state
  limited = m.actuator_actlimited[tid]
  range_low = wp.select(limited, -wp.inf, m.actuator_actrange[tid, 0])
  range_high = wp.select(limited, wp.inf, m.actuator_actrange[tid, 1])

  # get the actual actuation - skip if -1 (means stateless actuator)
  act_adr = m.actuator_actadr[tid]
  if act_adr == -1:
    return

  acts = d.act[worldId]
  acts_dot = act_dot_in[worldId]

  act = acts[act_adr]
  act_dot = acts_dot[act_adr]

  # check dynType
  dyn_type = m.actuator_dyntype[tid]
  dyn_prm = m.actuator_dynprm[tid, 0]

  # advance the actuation
  if dyn_type == 3:  # wp.static(WarpDynType.FILTEREXACT):
    tau = wp.select(
        dyn_prm < wp.static(WarpMjMinVal), dyn_prm, wp.static(WarpMjMinVal)
    )
    act = act + act_dot * tau * (1.0 - wp.exp(-m.timestep / tau))
  else:
    act = act + act_dot * m.timestep

  # apply limits
  wp.clamp(act, range_low, range_high)

  acts[act_adr] = act


@wp.kernel
def advance_velocities(
    m: types.Model, d: types.Data, qacc: wp.array2d(dtype=wp.float32)
):
  worldId, tid = wp.tid()
  d.qvel[worldId, tid] = d.qvel[worldId, tid] + qacc[worldId, tid] * m.timestep


@wp.kernel
def integrate_joint_positions(
    m: types.Model, d: types.Data, qvel_in: wp.array2d(dtype=wp.float32)
):
  worldId, tid = wp.tid()

  jnt_type = m.jnt_type[tid]
  qpos_adr = m.jnt_qposadr[tid]
  dof_adr = m.jnt_dofadr[tid]
  qpos = d.qpos[worldId]
  qvel = qvel_in[worldId]

  if jnt_type == 0:  # free joint
    qpos_pos = wp.vec3(qpos[qpos_adr], qpos[qpos_adr + 1], qpos[qpos_adr + 2])
    qvel_lin = wp.vec3(qvel[dof_adr], qvel[dof_adr + 1], qvel[dof_adr + 2])

    qpos_new = qpos_pos + m.timestep * qvel_lin

    qpos_quat = wp.quat(
        qpos[qpos_adr + 3],
        qpos[qpos_adr + 4],
        qpos[qpos_adr + 5],
        qpos[qpos_adr + 6],
    )
    qvel_ang = wp.vec3(qvel[dof_adr + 3], qvel[dof_adr + 4], qvel[dof_adr + 5])

    qpos_quat_new = quat_integrate_wp(qpos_quat, qvel_ang, m.timestep)

    qpos[qpos_adr] = qpos_new[0]
    qpos[qpos_adr + 1] = qpos_new[1]
    qpos[qpos_adr + 2] = qpos_new[2]
    qpos[qpos_adr + 3] = qpos_quat_new[0]
    qpos[qpos_adr + 4] = qpos_quat_new[1]
    qpos[qpos_adr + 5] = qpos_quat_new[2]
    qpos[qpos_adr + 6] = qpos_quat_new[3]

  elif jnt_type == 1:  # ball joint
    qpos_quat = wp.quat(
        qpos[qpos_adr],
        qpos[qpos_adr + 1],
        qpos[qpos_adr + 2],
        qpos[qpos_adr + 3],
    )
    qvel_ang = wp.vec3(qvel[dof_adr], qvel[dof_adr + 1], qvel[dof_adr + 2])

    qpos_quat_new = quat_integrate_wp(qpos_quat, qvel_ang, m.timestep)

    qpos[qpos_adr] = qpos_quat_new[0]
    qpos[qpos_adr + 1] = qpos_quat_new[1]
    qpos[qpos_adr + 2] = qpos_quat_new[2]
    qpos[qpos_adr + 3] = qpos_quat_new[3]

  else:  # if jnt_type in (JointType.HINGE, JointType.SLIDE):
    qpos[qpos_adr] = qpos[qpos_adr] + m.timestep * qvel[dof_adr]


def _advance(
    m: types.Model,
    d: types.Data,
    act_dot: wp.array,
    qacc: wp.array,
    qvel: Optional[wp.array] = None,
) -> types.Data:
  """Advance state and time given activation derivatives and acceleration."""

  # skip if no stateful actuators.
  if m.na:
    wp.launch(next_activation, dim=(d.nworld, m.nu), inputs=[m, d, act_dot])

  wp.launch(advance_velocities, dim=(d.nworld, m.nv), inputs=[m, d, qacc])

  # advance positions with qvel if given, d.qvel otherwise (semi-implicit)
  if qvel is not None:
    qvel_in = qvel
  else:
    qvel_in = d.qvel

  wp.launch(
      integrate_joint_positions, dim=(d.nworld, m.njnt), inputs=[m, d, qvel_in]
  )

  d.time = d.time + m.timestep

  return d


def euler(m: types.Model, d: types.Data) -> types.Data:
  """Euler integrator, semi-implicit in velocity."""
  # integrate damping implicitly

  @wp.kernel
  def add_damping_dense(m: types.Model, d: types.Data):
    worldId, tid = wp.tid()
    dof_Madr = m.dof_Madr[tid]
    d.qM[worldId, 0, dof_Madr] += m.timestep * m.dof_damping[dof_Madr]

  @wp.kernel
  def add_damping_sparse(m: types.Model, d: types.Data):
    worldId, tid = wp.tid()
    d.qM[worldId, tid, tid] += m.timestep * m.dof_damping[tid]

  @wp.kernel
  def sum_qfrc(m: types.Model, d: types.Data):
    worldId, tid = wp.tid()
    d.qfrc_eulerdamp[worldId, tid] = (
        d.qfrc_smooth[worldId, tid] + d.qfrc_constraint[worldId, tid]
    )

  wp.copy(d.qacc_eulerdamp, d.qacc)
  if not m.disable_flags & types.MJ_DSBL_EULERDAMP:
    
    if m.is_sparse:
      wp.launch(add_damping_dense, dim=(d.nworld, m.nM), inputs=[m, d])
    else:
      wp.launch(add_damping_sparse, dim=(d.nworld, m.nv), inputs=[m, d])
    wp.launch(sum_qfrc, dim=(d.nworld, m.nv), inputs=[m, d])

    smooth.factor_m(m, d)
    smooth.solve_m(m, d, d.qacc_eulerdamp, d.qfrc_eulerdamp)
  return _advance(m, d, d.act_dot, d.qacc_eulerdamp)
