import unittest
from collections import OrderedDict

import numpy as np

import PyKDL
import itertools
from tf.transformations import quaternion_matrix, quaternion_about_axis, quaternion_from_euler, euler_matrix, \
    rotation_matrix, quaternion_multiply, quaternion_conjugate, random_quaternion, quaternion_from_matrix, \
    quaternion_slerp

from numpy import pi

import giskardpy.symengine_wrappers as spw
from giskardpy import BACKEND

PKG = 'giskardpy'


class TestSympyWrapper(unittest.TestCase):
    def setUp(self):
        np.random.seed(23)

    def test_trace(self):
        for i in range(50):
            m1 = quaternion_matrix(random_quaternion())
            t1, t2 = spw.trace(m1), np.trace(m1)
            np.testing.assert_array_almost_equal(t1, t2)

    def test_q_to_m(self):
        q = spw.Matrix([0., 0., 0.14943813, 0.98877108])
        r1 = spw.rotation3_quaternion(*q)
        r1 = np.asarray(r1.tolist()).reshape(r1.shape).astype(float)[:3, :3]

        r_goal = quaternion_matrix(q)[:3, :3]

        r_goal2 = PyKDL.Rotation.Quaternion(*q)
        r_goal2 = np.array([r_goal2[x, y] for x, y in (itertools.product(range(3), repeat=2))]).reshape(3, 3)

        np.testing.assert_array_almost_equal(r1, r_goal)
        np.testing.assert_array_almost_equal(r1, r_goal2)

    def test_2(self):
        angle = .42
        axis = [0, 0, 1]
        r1 = spw.rotation3_axis_angle(axis, angle)
        r1 = np.asarray(r1.tolist()).reshape(r1.shape).astype(float)[:3, :3]

        r_goal = quaternion_matrix(quaternion_about_axis(angle, axis))[:3, :3]

        r_goal2 = PyKDL.Rotation.Rot(PyKDL.Vector(*axis), angle)
        r_goal2 = np.array([r_goal2[x, y] for x, y in (itertools.product(range(3), repeat=2))]).reshape(3, 3)

        np.testing.assert_array_almost_equal(r1, r_goal)
        np.testing.assert_array_almost_equal(r1, r_goal2)

    def test_3(self):
        r, p, y = .2, .7, -.3

        r1 = spw.rotation3_rpy(r, p, y)
        r1 = np.asarray(r1.tolist()).reshape(r1.shape)[:3, :3]

        r_goal = quaternion_matrix(quaternion_from_euler(r, p, y))[:3, :3]

        r_goal2 = PyKDL.Rotation.RPY(r, p, y)
        r_goal2 = np.array([r_goal2[x, y] for x, y in (itertools.product(range(3), repeat=2))]).reshape(3, 3)

        np.testing.assert_array_almost_equal(r1, r_goal)
        np.testing.assert_array_almost_equal(r1, r_goal2)

    def test_matrix_rpy(self):
        rpy = [[0, 0, 1],
               [0, 1, 1],
               [-1, 0, 1],
               [-0.2, 0, 0]]
        for r, p, y in rpy:
            m1 = np.array([x.evalf(real=True) for x in spw.rotation3_rpy(r, p, y)]).astype(float).reshape(4, 4)
            m2 = euler_matrix(r, p, y)
            np.testing.assert_array_almost_equal(m1, m2)

    def test_axis_angle(self):
        tests = [([0, 0, 1], np.pi / 2),
                 ([1, 0, 0], np.pi / 4),
                 ([1, 1, 1], np.pi / 1.2)]
        for axis, angle in tests:
            n_axis = np.array(axis) / np.linalg.norm(axis)
            m = spw.rotation3_axis_angle(n_axis, angle)
            new_axis, new_angle = spw.axis_angle_from_matrix(m)
            self.assertAlmostEqual(new_angle, angle)
            np.testing.assert_array_almost_equal(np.array(new_axis.T).astype(float)[0], n_axis)

    def test_axis_angle2(self):
        tests = [([0, 0, 1], np.pi / 2),
                 ([1, 0, 0], np.pi / 4),
                 ([1, 1, 1], np.pi / 1.2)]
        for axis, angle in tests:
            n_axis = np.array(axis) / np.linalg.norm(axis)
            q = spw.quaterntion_from_axis_angle(n_axis, angle)
            m = spw.rotation3_quaternion(*q)
            new_axis, new_angle = spw.axis_angle_from_matrix(m)
            self.assertAlmostEqual(new_angle, angle)
            np.testing.assert_array_almost_equal(np.array(new_axis.T).astype(float)[0], n_axis)

    def test_quaternion_from_axis_angle(self):
        for i in range(50):
            q = random_quaternion()
            spw_axis, spw_angle = spw.axis_angle_from_quaternion(q)
            spw_q = spw.quaterntion_from_axis_angle(spw_axis, spw_angle)
            spw_q = np.array(spw_q).astype(float).T[0]
            np.testing.assert_array_almost_equal(q, spw_q)
            tf_q = quaternion_about_axis(spw_angle, spw_axis)
            np.testing.assert_array_almost_equal(q, tf_q)
            kdl_q = np.array(PyKDL.Rotation.Rot(PyKDL.Vector(*spw_axis), spw_angle).GetQuaternion())
            try:
                np.testing.assert_array_almost_equal(q, kdl_q)
            except AssertionError as e:
                np.testing.assert_array_almost_equal(q, -kdl_q)

    def test_axis_angle3(self):
        rpy = [[0, 0, 1],
               [0, 1, 1],
               [-1, 0, 1],
               [-0.2, 0, 0]]
        for r, p, y in rpy:
            kdl_angle, kdl_axis = PyKDL.Rotation.RPY(r, p, y).GetRotAngle()

            spw_m = spw.rotation3_rpy(r, p, y)
            spw_axis, spw_angle = spw.axis_angle_from_matrix(spw_m)
            spw_axis = np.array([x.evalf(real=True) for x in spw_axis]).astype(float)
            spw_angle = spw_angle.evalf(real=True)
            self.assertAlmostEqual(kdl_angle, spw_angle)
            np.testing.assert_array_almost_equal([x for x in kdl_axis], spw_axis)

    def test_quaterntion_from_rpy1(self):
        rpy = [[0, 0, 1],
               [0, 1, 1],
               [-1, 0, 1],
               [-0.2, 0, 0],
               [0, 0, pi],
               [0.0, 1.57079632679, 0.0]]
        for r, p, y in rpy:
            q1 = np.array(spw.quaternion_from_rpy(r, p, y)).astype(float).T[0]
            q2 = quaternion_from_euler(r, p, y)
            q3 = PyKDL.Rotation.RPY(r, p, y).GetQuaternion()
            np.testing.assert_array_almost_equal(q1, q2)
            np.testing.assert_array_almost_equal(q1, q3)
            np.testing.assert_array_almost_equal(q2, q3)

    def test_quaterntion_from_rpy2(self):
        rpy = [[0, 0, 1],
               [0, 1, 1],
               [-1, 0, 1],
               [-0.2, 0, 0],
               [0, 0, pi]]
        for r, p, y in rpy:
            m1 = spw.rotation3_quaternion(*spw.quaternion_from_rpy(r, p, y))
            m1 = np.array([x.evalf(real=True) for x in m1]).astype(float).reshape(4, 4)
            m2 = spw.rotation3_rpy(r, p, y)
            m2 = np.array([x.evalf(real=True) for x in m2]).astype(float).reshape(4, 4)
            np.testing.assert_array_almost_equal(m1, m2)

    def test_quaternion_conjugate(self):
        for i in range(50):
            q = random_quaternion()
            q1_inv = np.array(spw.quaternion_conjugate(q)).astype(float).T[0]
            q1_inv2 = quaternion_conjugate(q)
            np.testing.assert_array_almost_equal(q1_inv, q1_inv2)

    def test_quaternion_diff(self):
        for i in range(50):
            q1 = random_quaternion()
            q2 = random_quaternion()
            m1 = spw.rotation3_quaternion(*q1)
            m2 = spw.rotation3_quaternion(*q2)
            m_diff = m1.T * m2
            q_diff = spw.quaternion_diff(q1, q2)
            m_q_diff = spw.rotation3_quaternion(*q_diff)

            m_diff = np.array(m_diff).astype(float)
            m_q_diff = np.array(m_q_diff).astype(float)
            np.testing.assert_array_almost_equal(m_diff, m_q_diff)

    def test_quaternion_from_matrix(self):
        for i in range(50):
            q1 = random_quaternion()

            q2 = spw.quaternion_from_matrix(spw.rotation3_quaternion(*q1))
            q2 = np.array(q2.T).astype(float)[0]

            q3 = quaternion_from_matrix(quaternion_matrix(q1))
            np.testing.assert_array_almost_equal(q2, q3)

    def test_quaternion_from_matrix2(self):
        a = spw.Symbol('a')
        b = spw.Symbol('b')
        c = spw.Symbol('c')
        d = spw.Symbol('d')
        expr = spw.quaternion_from_matrix(spw.rotation3_quaternion(a, b, c, d))
        expr = spw.speed_up(expr, [a, b, c, d], backend=BACKEND)
        for i in range(50):
            q1 = random_quaternion()

            q2 = expr(**{'a': q1[0],
                         'b': q1[1],
                         'c': q1[2],
                         'd': q1[3]})
            q2 = np.array(q2.T).astype(float)[0]

            q3 = quaternion_from_matrix(quaternion_matrix(q1))
            np.testing.assert_array_almost_equal(q2, q3)

    def test_fake_if1(self):
        for i in range(100):
            a = np.random.rand()
            b = np.random.rand()
            c = np.random.rand()
            result = spw.if_greater_zero(a, b, c)
            if a > 0:
                self.assertAlmostEqual(result, b)
            else:
                self.assertAlmostEqual(result, c)

    def test_fake_if2(self):
        muh = 9e9
        for i in range(100):
            a = np.random.rand() * muh
            b = np.random.rand() * muh
            c = np.random.rand() * muh
            result = spw.if_greater_zero(a, b, c)
            if a > 0:
                self.assertAlmostEqual(result, b, 5)
            else:
                self.assertAlmostEqual(result, c, 5)

    def test_fake_if_eq(self):
        for i in range(50):
            a = np.random.choice(range(-5, 5))
            b = np.random.rand()
            c = np.random.rand()
            result = spw.if_eq_zero(a, b, c)
            if a == 0:
                self.assertAlmostEqual(result, b, 5, msg='a:{} b:{} c:{}; should be {}'.format(a, b, c, b))
            else:
                self.assertAlmostEqual(result, c, 5, msg='a:{} b:{} c:{}; should be {}'.format(a, b, c, c))

    def test_slerp1(self):
        for i in range(50):
            from_ = random_quaternion()
            to_ = random_quaternion()
            t = round(np.random.rand(), 3)
            q1 = quaternion_slerp(from_, to_, t)
            q2 = spw.slerp2(spw.Matrix(from_), spw.Matrix(to_), t)
            q2 = np.array(q2).astype(float).T[0]
            np.testing.assert_array_almost_equal(q1, q2, err_msg='{} {} \nros:{} \ngeorg:{}'.format(from_, to_, q1, q2))

    def test_slerp2(self):
        q1 = spw.var('q1x q1y q1z q1w')
        q2 = spw.var('q2x q2y q2z q2w')
        t = spw.Symbol('t')
        q1_expr = spw.Matrix(q1)
        q2_expr = spw.Matrix(q2)
        expr = spw.slerp2(q1_expr, q2_expr, t)
        expr = spw.speed_up(expr, list(q1) + list(q2) + [t], backend=BACKEND)
        for i in range(50):
            from_ = random_quaternion()
            to_ = random_quaternion()
            t = round(np.random.rand(), 3)
            rq1 = quaternion_slerp(from_, to_, t)
            q2 = expr(**{
                'q1x': from_[0],
                'q1y': from_[1],
                'q1z': from_[2],
                'q1w': from_[3],

                'q2x': to_[0],
                'q2y': to_[1],
                'q2z': to_[2],
                'q2w': to_[3],
                't': t,
            })
            # rq2 = spw.slerp2(spw.Matrix(from_), spw.Matrix(to_), t)
            rq2 = np.array(q2).astype(float).T[0]
            np.testing.assert_array_almost_equal(rq1, rq2)


if __name__ == '__main__':
    import rosunit

    rosunit.unitrun(package=PKG,
                    test_name='TestSympyWrapper',
                    test=TestSympyWrapper)
