import numbers
import os
import traceback
from copy import deepcopy
from functools import cached_property

import numpy as np
import urdf_parser_py.urdf as up
from geometry_msgs.msg import PoseStamped, Pose, PointStamped, Point, Vector3Stamped, Vector3
from giskard_msgs.msg import CollisionEntry
from tf.transformations import euler_matrix
from visualization_msgs.msg import Marker, MarkerArray

from giskardpy import casadi_wrapper as w, RobotName, identifier
from giskardpy.data_types import JointStates, KeyDefaultDict, order_map
from giskardpy.data_types import PrefixName
from giskardpy.exceptions import RobotExistsException, DuplicateNameException, PhysicsWorldException, \
    UnknownBodyException, UnsupportedOptionException, CorruptShapeException
from giskardpy.god_map import GodMap
from giskardpy.model.joints import Joint, PrismaticJoint, RevoluteJoint, ContinuousJoint, MovableJoint, \
    FixedJoint, MimicJoint
from giskardpy.model.robot import Robot
from giskardpy.model.urdf_object import hacky_urdf_parser_fix
from giskardpy.model.utils import cube_volume, cube_surface, sphere_volume, cylinder_volume, cylinder_surface
from giskardpy.model.world_object import WorldObject
from giskardpy.utils import logging
from giskardpy.utils.tfwrapper import msg_to_kdl, kdl_to_pose, homo_matrix_to_pose, np_to_pose, pose_to_kdl, \
    kdl_to_np, pose_to_np, msg_to_homogeneous_matrix, np_point, np_vector
from giskardpy.utils.utils import suppress_stderr, memoize, resolve_ros_iris


class LinkGeometry(object):
    def __init__(self, link_T_geometry):
        self.link_T_geometry = link_T_geometry

    @classmethod
    def from_urdf(cls, urdf_thing):
        urdf_geometry = urdf_thing.geometry
        if urdf_thing.origin is None:
            link_T_geometry = np.eye(4)
        else:
            link_T_geometry = euler_matrix(*urdf_thing.origin.rpy)
            link_T_geometry[0, 3] = urdf_thing.origin.xyz[0]
            link_T_geometry[1, 3] = urdf_thing.origin.xyz[1]
            link_T_geometry[2, 3] = urdf_thing.origin.xyz[2]
        if isinstance(urdf_geometry, up.Mesh):
            geometry = MeshGeometry(link_T_geometry, urdf_geometry.filename, urdf_geometry.scale)
        elif isinstance(urdf_geometry, up.Box):
            geometry = BoxGeometry(link_T_geometry, *urdf_geometry.size)
        elif isinstance(urdf_geometry, up.Cylinder):
            geometry = CylinderGeometry(link_T_geometry, urdf_geometry.length, urdf_geometry.radius)
        elif isinstance(urdf_geometry, up.Sphere):
            geometry = SphereGeometry(link_T_geometry, urdf_geometry.radius)
        else:
            NotImplementedError('{} geometry is not supported'.format(type(urdf_geometry)))
        return geometry

    @classmethod
    def from_world_body(cls, msg):
        """
        :type msg: giskard_msgs.msg._WorldBody.WorldBody
        """
        if msg.type == msg.URDF_BODY:
            raise NotImplementedError()
        elif msg.type == msg.PRIMITIVE_BODY:
            if msg.shape.type == msg.shape.BOX:
                geometry = BoxGeometry(np.eye(4),
                                       depth=msg.shape.dimensions[msg.shape.BOX_X],
                                       width=msg.shape.dimensions[msg.shape.BOX_Y],
                                       height=msg.shape.dimensions[msg.shape.BOX_Z])
            elif msg.shape.type == msg.shape.CYLINDER:
                geometry = CylinderGeometry(np.eye(4),
                                            height=msg.shape.dimensions[msg.shape.CYLINDER_HEIGHT],
                                            radius=msg.shape.dimensions[msg.shape.CYLINDER_RADIUS])
            elif msg.shape.type == msg.shape.SPHERE:
                geometry = SphereGeometry(np.eye(4),
                                          radius=msg.shape.dimensions[msg.shape.SPHERE_RADIUS])
        elif msg.type == msg.MESH_BODY:
            geometry = MeshGeometry(np.eye(4), msg.mesh)
        else:
            raise ValueError('World body type {} not supported'.format(msg.type))
        return geometry

    def as_visualization_marker(self):
        marker = Marker()
        marker.color.a = 0.5
        marker.color.r = 1.0
        marker.color.g = 1.0
        marker.color.b = 1.0

        marker.pose = Pose()
        marker.pose = np_to_pose(self.link_T_geometry)
        return marker

    def is_big(self, volume_threshold=1.001e-6, surface_threshold=0.00061):
        return False


class MeshGeometry(LinkGeometry):
    def __init__(self, link_T_geometry, file_name, scale=None):
        super(MeshGeometry, self).__init__(link_T_geometry)
        self.file_name = file_name
        if not os.path.isfile(resolve_ros_iris(file_name)):
            raise CorruptShapeException('Can\'t find file {}'.format(self.file_name))
        if scale is None:
            self.scale = [1, 1, 1]
        else:
            self.scale = scale

    def as_visualization_marker(self):
        marker = super(MeshGeometry, self).as_visualization_marker()
        marker.type = Marker.MESH_RESOURCE
        marker.mesh_resource = self.file_name
        marker.scale.x = self.scale[0]
        marker.scale.z = self.scale[1]
        marker.scale.y = self.scale[2]
        marker.mesh_use_embedded_materials = True
        return marker

    def as_urdf(self):
        return up.Mesh(self.file_name, self.scale)

    def is_big(self, volume_threshold=1.001e-6, surface_threshold=0.00061):
        return True


class BoxGeometry(LinkGeometry):
    def __init__(self, link_T_geometry, depth, width, height):
        super(BoxGeometry, self).__init__(link_T_geometry)
        self.depth = depth
        self.width = width
        self.height = height

    def as_visualization_marker(self):
        marker = super(BoxGeometry, self).as_visualization_marker()
        marker.type = Marker.CUBE
        marker.scale.x = self.depth
        marker.scale.y = self.width
        marker.scale.z = self.height
        return marker

    def as_urdf(self):
        return up.Box([self.depth, self.width, self.height])

    def is_big(self, volume_threshold=1.001e-6, surface_threshold=0.00061):
        return (cube_volume(self.depth, self.width, self.height) > volume_threshold or
                cube_surface(self.depth, self.width, self.height) > surface_threshold)


class CylinderGeometry(LinkGeometry):
    def __init__(self, link_T_geometry, height, radius):
        super(CylinderGeometry, self).__init__(link_T_geometry)
        self.height = height
        self.radius = radius

    def as_visualization_marker(self):
        marker = super(CylinderGeometry, self).as_visualization_marker()
        marker.type = Marker.CYLINDER
        marker.scale.x = self.radius * 2
        marker.scale.y = self.radius * 2
        marker.scale.z = self.height
        return marker

    def as_urdf(self):
        return up.Cylinder(self.radius, self.height)

    def is_big(self, volume_threshold=1.001e-6, surface_threshold=0.00061):
        return (cylinder_volume(self.radius, self.height) > volume_threshold or
                cylinder_surface(self.radius, self.height) > surface_threshold)


class SphereGeometry(LinkGeometry):
    def __init__(self, link_T_geometry, radius):
        super(SphereGeometry, self).__init__(link_T_geometry)
        self.radius = radius

    def as_visualization_marker(self):
        marker = super(SphereGeometry, self).as_visualization_marker()
        marker.type = Marker.SPHERE
        marker.scale.x = self.radius * 2
        marker.scale.y = self.radius * 2
        marker.scale.z = self.radius * 2
        return marker

    def as_urdf(self):
        return up.Sphere(self.radius)

    def is_big(self, volume_threshold=1.001e-6, surface_threshold=0.00061):
        return sphere_volume(self.radius) > volume_threshold


class Link(object):
    def __init__(self, name):
        self.name = name  # type: PrefixName
        self.visuals = []
        self.collisions = []
        self.parent_joint_name = None
        self.child_joint_names = []

    @classmethod
    def from_urdf(cls, urdf_link, prefix):
        link_name = PrefixName(urdf_link.name, prefix)
        link = cls(link_name)
        for urdf_collision in urdf_link.collisions:
            link.collisions.append(LinkGeometry.from_urdf(urdf_collision))
        for urdf_visual in urdf_link.visuals:
            link.visuals.append(LinkGeometry.from_urdf(urdf_visual))
        return link

    @classmethod
    def from_world_body(cls, msg):
        """
        :type msg: giskard_msgs.msg._WorldBody.WorldBody
        :type pose: Pose
        """
        link_name = PrefixName(msg.name, None)
        link = cls(link_name)
        geometry = LinkGeometry.from_world_body(msg)
        link.collisions.append(geometry)
        link.visuals.append(geometry)
        return link

    def collision_visualization_markers(self):
        markers = MarkerArray()
        for collision in self.collisions:  # type: LinkGeometry
            marker = collision.as_visualization_marker()
            markers.markers.append(marker)
        return markers

    def as_urdf(self):
        r = up.Robot(self.name)
        r.version = u'1.0'
        link = up.Link(self.name)
        # if self.visuals:
        #     link.add_aggregate(u'visual', up.Visual(self.visuals[0].as_urdf()))
        link.add_aggregate(u'collision', up.Collision(self.collisions[0].as_urdf()))
        r.add_link(link)
        return r.to_xml_string()

    def has_visuals(self):
        return len(self.visuals) > 0

    def has_collisions(self, volume_threshold=1.001e-6, surface_threshold=0.00061):
        """
        :type link: str
        :param volume_threshold: m**3, ignores simple geometry shapes with a volume less than this
        :type volume_threshold: float
        :param surface_threshold: m**2, ignores simple geometry shapes with a surface area less than this
        :type surface_threshold: float
        :return: True if collision geometry is mesh or simple shape with volume/surface bigger than thresholds.
        :rtype: bool
        """
        for collision in self.collisions:
            geo = collision
            if geo.is_big():
                return True
        return False

    def __repr__(self):
        return str(self.name)


class WorldTree(object):
    def __init__(self, god_map=None):
        self.god_map = god_map  # type: GodMap
        self.connection_prefix = 'connection'
        self.fast_all_fks = None
        self._version = 0
        self.hard_reset()

    @property
    def version(self):
        return self._version

    def _increase_version(self):
        self.soft_reset()
        self._version += 1

    def search_branch(self, joint_name,
                      stop_at_joint_when=None, stop_at_link_when=None,
                      collect_joint_when=None, collect_link_when=None):
        """

        :param joint_name:
        :param stop_at_joint_when: If None, 'lambda joint_name: False' is used.
        :param stop_at_link_when: If None, 'lambda joint_name: False' is used.
        :param collect_joint_when: If None, 'lambda joint_name: False' is used.
        :param collect_link_when: If None, 'lambda joint_name: False' is used.
        :return: Collected links and joints. DOES NOT INCLUDE joint_name
        """
        if stop_at_joint_when is None:
            def stop_at_joint_when(_):
                return False
        if stop_at_link_when is None:
            def stop_at_link_when(_):
                return False
        if collect_joint_when is None:
            def collect_joint_when(_):
                return False
        if collect_link_when is None:
            def collect_link_when(_):
                return False

        def helper(joint_name):
            joint = self.joints[joint_name]
            collected_link_names = []
            collected_joint_names = []
            child_link = self.links[joint.child_link_name]
            if collect_link_when(child_link.name):
                collected_link_names.append(child_link.name)
            if not stop_at_link_when(child_link.name):
                for child_joint_name in child_link.child_joint_names:
                    if collect_joint_when(child_joint_name):
                        collected_joint_names.append(child_joint_name)
                    if stop_at_joint_when(child_joint_name):
                        continue
                    links_to_add, joints_to_add = helper(child_joint_name)
                    collected_link_names.extend(links_to_add)
                    collected_joint_names.extend(joints_to_add)
            return collected_link_names, collected_joint_names

        return helper(joint_name)

    @memoize
    def get_directly_controlled_child_links_with_collisions(self, joint_name):
        links, joints = self.search_branch(joint_name,
                                           stop_at_joint_when=self.is_joint_controlled,
                                           collect_link_when=self.has_link_collisions)
        return links

    def get_siblings_with_collisions(self, joint_name):
        """
        Goes up the tree until the first controlled joint and then down again until another controlled joint or
        the joint_name is reached again. Collects all links with collision along the way.
        :param joint_name:
        :return:
        """
        try:
            parent_joint = self.search_for_parent_joint(joint_name, stop_when=self.is_joint_controlled)
        except KeyError as e:
            return []

        def stop_at_joint_when(other_joint_name):
            return joint_name == other_joint_name or self.is_joint_controlled(other_joint_name)

        links, joints = self.search_branch(parent_joint,
                                           stop_at_joint_when=stop_at_joint_when,
                                           collect_link_when=self.has_link_collisions)
        return links

    def reset_cache(self):
        # FIXME this sucks because it calls properties
        for method_name in dir(self):
            try:
                getattr(self, method_name).memo.clear()
            except:
                pass

    def register_group(self, name, root_link_name):
        if root_link_name not in self.links:
            raise KeyError('World doesn\'t have link \'{}\''.format(root_link_name))
        if name in self.groups:
            raise DuplicateNameException('Group with name {} already exists'.format(name))
        self.groups[name] = SubWorldTree(name, root_link_name, self)
        self._increase_version()

    @property
    def group_names(self):
        return set(self.groups.keys())

    @property
    def minimal_group_names(self):
        group_names = self.group_names
        for group in self.groups.values():
            for group_name in group.group_names:
                group_names.remove(group_name)
        return group_names

    @property
    def root_link(self):
        return self.links[self.root_link_name]

    @cached_property
    def link_names(self):
        return set(self.links.keys())

    @property
    def link_names_with_visuals(self):
        return set(link.name for link in self.links.values() if link.has_visuals())

    @cached_property
    def link_names_with_collisions(self):
        return set(link.name for link in self.links.values() if link.has_collisions())

    @property
    def joint_names(self):
        return list(self.joints.keys())

    def add_urdf(self, urdf, prefix=None, parent_link_name=None, group_name=None):
        # create group?
        with suppress_stderr():
            parsed_urdf = up.URDF.from_xml_string(hacky_urdf_parser_fix(urdf))  # type: up.Robot
        if group_name in self.groups:
            raise DuplicateNameException(
                'Failed to add group \'{}\' because one with such a name already exists'.format(group_name))
        if parent_link_name is None:
            parent_link = self.root_link
        else:
            parent_link = self.links[parent_link_name]
        child_link = Link(name=PrefixName(parsed_urdf.get_root(), prefix))
        connecting_joint = FixedJoint(name=PrefixName(PrefixName(parsed_urdf.name, prefix), self.connection_prefix),
                                      parent_link_name=parent_link.name,
                                      child_link_name=child_link.name)
        self.link_joint_to_links(connecting_joint, child_link)

        def helper(urdf, parent_link):
            short_name = parent_link.name.short_name
            if short_name not in urdf.child_map:
                return
            for child_joint_name, child_link_name in urdf.child_map[short_name]:
                urdf_link = urdf.link_map[child_link_name]
                child_link = Link.from_urdf(urdf_link, prefix)

                urdf_joint = urdf.joint_map[child_joint_name]
                joint = Joint.from_urdf(urdf_joint, prefix, parent_link.name, child_link.name, self.god_map)

                self.link_joint_to_links(joint, child_link)
                helper(urdf, child_link)

        helper(parsed_urdf, child_link)
        if group_name is not None:
            self.register_group(group_name, child_link.name)
        self.sync_with_paramserver()

    def get_parent_link_of_link(self, link_name):
        """
        :type link_name: PrefixName
        :rtype: PrefixName
        """
        return self.joints[self.links[link_name].parent_joint_name].parent_link_name

    @memoize
    def compute_chain_reduced_to_controlled_joints(self, link_a, link_b):
        chain1, connection, chain2 = self.compute_split_chain(link_b, link_a, joints=True, links=True, fixed=True,
                                                              non_controlled=True)
        chain = chain1 + connection + chain2
        for i, thing in enumerate(chain):
            if i % 2 == 1 and thing in self.controlled_joints:
                new_link_b = chain[i - 1]
                break
        else:
            raise KeyError(u'no controlled joint in chain between {} and {}'.format(link_a, link_b))
        for i, thing in enumerate(reversed(chain)):
            if i % 2 == 1 and thing in self.controlled_joints:
                new_link_a = chain[len(chain) - i]
                break
        else:
            raise KeyError(u'no controlled joint in chain between {} and {}'.format(link_a, link_b))
        return new_link_a, new_link_b

    @memoize
    def get_movable_parent_joint(self, link_name):
        joint = self.links[link_name].parent_joint_name
        while not self.is_joint_movable(joint):
            joint = self.links[self.joints[joint].parent_link_name].parent_joint_name
        return joint

    def add_world_body(self, msg, pose, parent_link_name=None):
        """
        :type msg: giskard_msgs.msg._WorldBody.WorldBody
        :type pose: Pose
        """
        if parent_link_name is None:
            parent_link = self.root_link
        else:
            parent_link = self.links[parent_link_name]
        if msg.name in self.links:
            raise DuplicateNameException('Link with name {} already exists'.format(msg.name))
        if msg.name in self.joints:
            raise DuplicateNameException('Joint with name {} already exists'.format(msg.name))
        if msg.type == msg.URDF_BODY:
            self.add_urdf(urdf=msg.urdf,
                          parent_link_name=parent_link.name,
                          group_name=msg.name,
                          prefix=None)
        else:
            link = Link.from_world_body(msg)
            joint = FixedJoint(PrefixName(msg.name, self.connection_prefix), parent_link.name, link.name,
                               parent_T_child=w.Matrix(kdl_to_np(pose_to_kdl(pose))))
            self.link_joint_to_links(joint, link)
            self.register_group(msg.name, link.name)
        self._increase_version()

    @property
    def movable_joints(self):
        return [j.name for j in self.joints.values() if isinstance(j, MovableJoint)]

    def soft_reset(self):
        self.reset_cache()
        self.init_fast_fks()
        for group in self.groups.values():
            group.soft_reset()
        del self.link_names
        del self.link_names_with_collisions

    # def get_controlled_links(self):
    #     # FIXME expensive
    #     if not self._controlled_links:
    #         self._controlled_links = set()
    #         for joint_name in self.controlled_joints:
    #             self._controlled_links.update(self.get_sub_tree_link_names_with_collision(joint_name))
    #     return self._controlled_links

    def hard_reset(self):
        self.state = JointStates()
        self.root_link_name = PrefixName(self.god_map.unsafe_get_data(identifier.map_frame), None)
        self.links = {self.root_link_name: Link(self.root_link_name)}
        self.joints = {}
        self.groups = {}
        try:
            self.add_urdf(self.god_map.unsafe_get_data(identifier.robot_description), group_name=RobotName, prefix=None)
        except KeyError:
            logging.logwarn('Can\'t add robot, because it is not on the param server')
        self.fast_all_fks = None
        self.soft_reset()

    def sync_with_paramserver(self):
        # FIXME this is probable being called repeatedly, creating huge min max expressions over time
        for i in range(1, self.god_map.unsafe_get_data(identifier.order)):
            order_identifier = identifier.joint_limits + [order_map[i]]
            d_linear = KeyDefaultDict(lambda key: self.god_map.to_symbol(order_identifier +
                                                                         [u'linear', u'override', key]))
            d_angular = KeyDefaultDict(lambda key: self.god_map.to_symbol(order_identifier +
                                                                          [u'angular', u'override', key]))
            self.set_joint_limits(d_linear, d_angular, i)
        for i in range(1, self.god_map.unsafe_get_data(identifier.order)):
            def default(joint_name):
                return self.god_map.to_symbol(identifier.joint_weights + [order_map[i], 'override', joint_name])
            d = KeyDefaultDict(default)
            self.set_joint_weights(i, d)
        self._increase_version()

    @property
    def joint_constraints(self):
        return {j.name: j.free_variable for j in self.joints.values() if j.has_free_variables()}

    def link_joint_to_links(self, joint, child_link):
        """
        :type joint: Joint
        :type child_link: Link
        """
        parent_link = self.links[joint.parent_link_name]
        if joint.name in self.joints:
            raise DuplicateNameException('Cannot add joint named \'{}\' because already exists'.format(joint.name))
        self.joints[joint.name] = joint
        child_link.parent_joint_name = joint.name
        if child_link.name in self.links:
            raise DuplicateNameException('Cannot add link named \'{}\' because already exists'.format(child_link.name))
        self.links[child_link.name] = child_link
        assert joint.name not in parent_link.child_joint_names
        parent_link.child_joint_names.append(joint.name)

    def move_branch(self, joint_name, new_parent_link_name):
        if not self.is_joint_fixed(joint_name):
            raise NotImplementedError('Can only change fixed joints')
        joint = self.joints[joint_name]
        fk = w.Matrix(self.compute_fk_np(new_parent_link_name, joint.child_link_name))
        old_parent_link = self.links[joint.parent_link_name]
        new_parent_link = self.links[new_parent_link_name]

        joint.parent_link_name = new_parent_link_name
        joint.parent_T_child = fk
        old_parent_link.child_joint_names.remove(joint_name)
        new_parent_link.child_joint_names.append(joint_name)
        self._increase_version()

    def move_group(self, group_name, new_parent_link_name):
        group = self.groups[group_name]
        joint_name = self.links[group.root_link_name].parent_joint_name
        if self.joints[joint_name].parent_link_name == new_parent_link_name:
            raise DuplicateNameException(
                '\'{}\' is already attached to \'{}\''.format(group_name, new_parent_link_name))
        self.move_branch(joint_name, new_parent_link_name)

    def delete_group(self, group_name):
        self.delete_branch(self.groups[group_name].root_link_name)

    def delete_branch(self, link_name):
        self.delete_branch_at_joint(self.links[link_name].parent_joint_name)

    def delete_branch_at_joint(self, joint_name):
        joint = self.joints.pop(joint_name)  # type: Joint
        self.links[joint.parent_link_name].child_joint_names.remove(joint_name)

        def helper(link_name):
            link = self.links.pop(link_name)
            for group_name in list(self.groups.keys()):
                if self.groups[group_name].root_link_name == link_name:
                    del self.groups[group_name]
                    logging.loginfo('Deleted group \'{}\', because it\'s root link got removed.'.format(group_name))
            for child_joint_name in link.child_joint_names:
                child_joint = self.joints.pop(child_joint_name)  # type: Joint
                helper(child_joint.child_link_name)

        helper(joint.child_link_name)
        self._increase_version()

    def link_order(self, link_a, link_b):
        """
        TODO find a better name
        this function is used when deciding for which order to calculate the collisions
        true if link_a < link_b
        :type link_a: str
        :type link_b: str
        :rtype: bool
        """
        try:
            self.get_controlled_parent_joint_of_link(link_a)
        except KeyError:
            return False
        try:
            self.get_controlled_parent_joint_of_link(link_b)
        except KeyError:
            return True
        return link_a < link_b

    @property
    def controlled_joints(self):
        return self.god_map.unsafe_get_data(identifier.controlled_joints)

    @memoize
    def get_controlled_parent_joint_of_link(self, link_name):
        joint = self.links[link_name].parent_joint_name
        if self.is_joint_controlled(joint):
            return joint
        return self.get_controlled_parent_joint_of_joint(joint)

    @memoize
    def get_controlled_parent_joint_of_joint(self, joint_name):
        return self.search_for_parent_joint(joint_name, self.is_joint_controlled)

    def search_for_parent_joint(self, joint_name, stop_when=None):
        try:
            joint = self.links[self.joints[joint_name].parent_link_name].parent_joint_name
            while stop_when is not None and not stop_when(joint):
                joint = self.search_for_parent_joint(joint)
        except KeyError as e:
            raise KeyError('\'{}\' has no fitting parent joint'.format(joint_name))
        return joint

    @profile
    def compute_chain(self, root_link_name, tip_link_name, joints, links, fixed, non_controlled):
        # FIXME memoizing this function results in weird errors...
        chain = []
        if links:
            chain.append(tip_link_name)
        link = self.links[tip_link_name]
        while link.name != root_link_name:
            if link.parent_joint_name not in self.joints:
                raise ValueError('{} and {} are not connected'.format(root_link_name, tip_link_name))
            parent_joint = self.joints[link.parent_joint_name]
            parent_link = self.links[parent_joint.parent_link_name]
            if joints:
                if (fixed or not isinstance(parent_joint, FixedJoint)) and \
                        (non_controlled or parent_joint.name in self.controlled_joints):
                    chain.append(parent_joint.name)
            if links:
                chain.append(parent_link.name)
            link = parent_link
        chain.reverse()
        return chain

    @memoize
    def compute_split_chain(self, root, tip, joints, links, fixed, non_controlled):
        if root == tip:
            return [], [], []
        root_chain = self.compute_chain(self.root_link_name, root, False, True, True, True)
        tip_chain = self.compute_chain(self.root_link_name, tip, False, True, True, True)
        for i in range(min(len(root_chain), len(tip_chain))):
            if root_chain[i] != tip_chain[i]:
                break
        else:
            i += 1
        connection = tip_chain[i - 1]
        root_chain = self.compute_chain(connection, root, joints, links, fixed, non_controlled)
        if links:
            root_chain = root_chain[1:]
        root_chain.reverse()
        tip_chain = self.compute_chain(connection, tip, joints, links, fixed, non_controlled)
        if links:
            tip_chain = tip_chain[1:]
        return root_chain, [connection] if links else [], tip_chain

    def compose_fk_expression(self, root_link, tip_link):
        fk = w.eye(4)
        root_chain, _, tip_chain = self.compute_split_chain(root_link, tip_link, joints=True, links=False, fixed=True,
                                                            non_controlled=True)
        for joint_name in root_chain:
            fk = w.dot(fk, w.inverse_frame(self.joints[joint_name].parent_T_child))
        for joint_name in tip_chain:
            fk = w.dot(fk, self.joints[joint_name].parent_T_child)
        # FIXME there is some reference fuckup going on, but i don't know where; deepcopy is just a quick fix
        return deepcopy(fk)

    @profile
    def init_fast_fks(self):
        def f(key):
            root, tip = key
            fk = self.compose_fk_expression(root, tip)
            m = w.speed_up(fk, w.free_symbols(fk))
            return m

        self._fks = KeyDefaultDict(f)

    @memoize
    def compute_fk_pose(self, root, tip):
        try:
            homo_m = self.compute_fk_np(root, tip)
            p = PoseStamped()
            p.header.frame_id = str(root)
            p.pose = homo_matrix_to_pose(homo_m)
        except Exception as e:
            print(e)
            traceback.print_exc()
            pass
        return p

    @memoize
    @profile
    def compute_fk_np(self, root, tip):
        return self._fks[root, tip].call2(self.god_map.unsafe_get_values(self._fks[root, tip].str_params))

    @profile
    def compute_all_fks(self):
        # TODO speedup possible
        # def helper(link, root_T_parent):
        #     if link.parent_joint_name in self.joints:
        #         root_T_link = root_T_parent * self.joints[link.parent_joint_name].parent_T_child
        #     else:
        #         root_T_link = root_T_parent
        #     if link.has_collisions():
        #         fks = {link.name: root_T_link}
        #     else:
        #         fks = {}
        #     for child_joint_name in link.child_joint_names:
        #         child_link = self.joints[child_joint_name].child
        #         fks.update(helper(child_link, root_T_link))
        #     return fks
        # fks_dict = helper(self.root_link, w.eye(4))
        if self.fast_all_fks is None:
            fks = []
            self.fk_idx = {}
            i = 0
            for link in self.links.values():
                if link.name == self.root_link_name:
                    continue
                if link.has_collisions():
                    fk = self.compose_fk_expression(self.root_link_name, link.name)
                    fk = w.dot(fk, link.collisions[0].link_T_geometry)
                    position = w.position_of(fk)
                    orientation = w.quaternion_from_matrix(fk)
                    fks.append(w.vstack([position, orientation]).T)
                    self.fk_idx[link.name] = i
                    i += 1
            fks = w.vstack(fks)
            self.fast_all_fks = w.speed_up(fks, w.free_symbols(fks))

        fks_evaluated = self.fast_all_fks.call2(self.god_map.unsafe_get_values(self.fast_all_fks.str_params))
        result = {}
        for link in self.link_names_with_collisions:
            result[link] = fks_evaluated[self.fk_idx[link], :]
        return result

    @profile
    def compute_all_fks_matrix(self):
        fks = []
        # self.fk_idx = {}
        # i = 0
        for link in self.links.values():
            if link.name == self.root_link_name:
                continue
            if link.has_collisions():
                map_T_o = self.compose_fk_expression(self.root_link_name, link.name)
                map_T_geo = w.dot(map_T_o, link.collisions[0].link_T_geometry)
                fks.append(map_T_geo)
                # self.fk_idx[link.name] = i
                # i += 4
        fks = w.vstack(fks)
        fast_all_fks = w.speed_up(fks, w.free_symbols(fks))

        class ComputeFKs(object):
            def __init__(self, f, god_map):
                self.f = f
                self.god_map = god_map

            @profile
            def __call__(self):
                return fast_all_fks.call2(self.god_map.unsafe_get_values(self.f.str_params))

        return ComputeFKs(fast_all_fks, self.god_map)
        # result = {}
        # for link in self.link_names_with_collisions:
        #     result[link] = fks_evaluated[self.fk_idx[link], :]
        # return result

    @memoize
    def are_linked(self, link_a, link_b, non_controlled=False, fixed=False):
        """
        Return True if all joints between link_a and link_b are fixed.
        :type link_a: str
        :type link_b: str
        :rtype: bool
        """
        chain1, connection, chain2 = self.compute_split_chain(link_a, link_b, joints=True, links=False, fixed=fixed,
                                                              non_controlled=non_controlled)
        return not chain1 and not connection and not chain2

    def set_joint_limits(self, linear_limits, angular_limits, order):
        for joint in self.joints.values():
            if self.is_joint_fixed(joint.name) or self.is_joint_mimic(joint.name):
                continue
            if self.is_joint_rotational(joint.name):
                new_limits = angular_limits
            else:
                new_limits = linear_limits

            old_upper_limits = joint.free_variable.upper_limits[order]
            old_upper_limits_str = str(old_upper_limits)
            if old_upper_limits_str.startswith('fmin') or old_upper_limits_str.startswith('fmax'):
                continue
            if old_upper_limits is None:
                joint.free_variable.upper_limits[order] = new_limits[joint.name]
            else:
                joint.free_variable.upper_limits[order] = w.min(old_upper_limits,
                                                                new_limits[joint.name])

            old_lower_limits = joint.free_variable.lower_limits[order]
            if old_lower_limits is None:
                joint.free_variable.lower_limits[order] = new_limits[joint.name]
            else:
                joint.free_variable.lower_limits[order] = w.max(old_lower_limits,
                                                                -new_limits[joint.name])

    def set_joint_weights(self, order, weights):
        for joint_name, joint in self.joints.items():
            if self.is_joint_movable(joint_name) and not self.is_joint_mimic(joint_name):
                joint.free_variable.quadratic_weights[order] = weights[joint_name]

    def joint_limit_expr(self, joint_name, order):
        upper_limit = self.joints[joint_name].free_variable.get_upper_limit(order)
        lower_limit = self.joints[joint_name].free_variable.get_lower_limit(order)
        return lower_limit, upper_limit

    def transform_msg(self, target_frame, msg):
        if isinstance(msg, PoseStamped):
            return self.transform_pose(target_frame, msg)
        elif isinstance(msg, PointStamped):
            return self.transform_point(target_frame, msg)
        elif isinstance(msg, Vector3Stamped):
            return self.transform_vector(target_frame, msg)
        else:
            raise NotImplementedError('World can\'t transform message of type \'{}\''.format(type(msg)))

    def transform_pose(self, target_frame, pose):
        """
        :type target_frame: Union[str, PrefixName]
        :type pose: PoseStamped
        :rtype: PoseStamped
        """
        f_T_p = msg_to_homogeneous_matrix(pose.pose)
        t_T_f = self.compute_fk_np(target_frame, pose.header.frame_id)
        t_T_p = np.dot(t_T_f, f_T_p)
        result = PoseStamped()
        result.header.frame_id = target_frame
        result.pose = np_to_pose(t_T_p)
        return result

    def transform_point(self, target_frame, point):
        """
        :type target_frame: Union[str, PrefixName]
        :type point: PointStamped
        :rtype: PointStamped
        """
        f_P_p = np_point(point.point.x, point.point.y, point.point.z)
        t_T_f = self.compute_fk_np(target_frame, point.header.frame_id)
        t_P_p = np.dot(t_T_f, f_P_p)
        result = PointStamped()
        result.header.frame_id = target_frame
        result.point = Point(*t_P_p[:3])
        return result

    def transform_vector(self, target_frame, vector):
        """
        :type target_frame: Union[str, PrefixName]
        :type vector: Vector3Stamped
        :rtype: Vector3Stamped
        """
        f_V_p = np_vector(vector.vector.x, vector.vector.y, vector.vector.z)
        t_T_f = self.compute_fk_np(target_frame, vector.header.frame_id)
        t_V_p = np.dot(t_T_f, f_V_p)
        result = Vector3Stamped()
        result.header.frame_id = target_frame
        result.vector = Vector3(*t_V_p[:3])
        return result

    def compute_joint_limits(self, joint_name, order):
        lower_limit, upper_limit = self.joint_limit_expr(joint_name, order)
        if not isinstance(lower_limit, numbers.Number) and lower_limit is not None:
            f = w.speed_up(lower_limit, w.free_symbols(lower_limit))
            lower_limit = f.call2(self.god_map.get_values(f.str_params))[0][0]
        if not isinstance(upper_limit, numbers.Number) and upper_limit is not None:
            f = w.speed_up(upper_limit, w.free_symbols(upper_limit))
            upper_limit = f.call2(self.god_map.get_values(f.str_params))[0][0]
        return lower_limit, upper_limit

    def get_joint_position_limits(self, joint_name):
        """
        :return: minimum position, maximum position as float
        """
        return self.compute_joint_limits(joint_name, 0)

    def get_joint_velocity_limits(self, joint_name):
        return self.compute_joint_limits(joint_name, 1)

    def get_all_joint_position_limits(self):
        return {j: self.get_joint_position_limits(j) for j in self.movable_joints}

    def is_joint_prismatic(self, joint_name):
        return isinstance(self.joints[joint_name], PrismaticJoint)

    def is_joint_fixed(self, joint_name):
        return not isinstance(self.joints[joint_name], MovableJoint)

    def is_joint_movable(self, joint_name):
        return not self.is_joint_fixed(joint_name)

    def is_joint_controlled(self, joint_name):
        return joint_name in self.controlled_joints

    def is_joint_revolute(self, joint_name):
        return isinstance(self.joints[joint_name], RevoluteJoint)

    def is_joint_continuous(self, joint_name):
        return isinstance(self.joints[joint_name], ContinuousJoint)

    def is_joint_mimic(self, joint_name):
        return isinstance(self.joints[joint_name], MimicJoint)

    def is_joint_rotational(self, joint_name):
        return self.is_joint_revolute(joint_name) or self.is_joint_continuous(joint_name)

    def has_joint(self, joint_name):
        return joint_name in self.joints

    def has_link_collisions(self, link_name):
        return self.links[link_name].has_collisions()

    def has_link_visuals(self, link_name):
        return self.links[link_name].has_visuals()


class SubWorldTree(WorldTree):
    def __init__(self, name, root_link_name, world):
        """
        :type name: str
        :type root_link_name: PrefixName
        :type world: WorldTree
        """
        self.name = name
        self.root_link_name = root_link_name
        self.world = world

    def hard_reset(self):
        raise NotImplementedError('Can\'t hard reset a SubWorldTree.')

    @property
    def base_pose(self):
        return self.world.compute_fk_pose(self.world.root_link_name, self.root_link_name).pose

    @property
    def _fks(self):
        return self.world._fks

    def soft_reset(self):
        self.reset_cache()

    @property
    def state(self):
        """
        :rtype: JointStates
        """
        return JointStates({j: self.world.state[j] for j in self.joints if j in self.world.state})

    @state.setter
    def state(self, value):
        self.world.state = value

    def reset_cache(self):
        super(SubWorldTree, self).reset_cache()
        del self.joints
        del self.links
        del self.link_names
        del self.link_names_with_collisions
        del self.groups

    @property
    def god_map(self):
        return self.world.god_map

    @property
    def root_link(self):
        return self.world.links[self.root_link_name]

    @cached_property
    def joints(self):
        def helper(root_link):
            """
            :type root_link: Link
            :rtype: dict
            """
            joints = {j: self.world.joints[j] for j in root_link.child_joint_names}
            for j in root_link.child_joint_names:  # type: Joint
                j = self.world.joints[j]
                child_link = self.world.links[j.child_link_name]
                joints.update(helper(child_link))
            return joints

        return helper(self.root_link)

    @cached_property
    def groups(self):
        return {group_name: group for group_name, group in self.world.groups.items() if
                group.root_link_name in self.links and group.name != self.name}

    @cached_property
    def links(self):
        def helper(root_link):
            """
            :type root_link: Link
            :rtype: list
            """
            links = {root_link.name: root_link}
            for j in root_link.child_joint_names:  # type: Joint
                j = self.world.joints[j]
                child_link = self.world.links[j.child_link_name]
                links.update(helper(child_link))
            return links

        return helper(self.root_link)

    def register_group(self, name, root_link_name):
        raise NotImplementedError()

    def link_joint_to_links(self, joint):
        raise NotImplementedError()

    def add_urdf_joint(self, urdf_joint):
        raise NotImplementedError()

    def delete_branch(self, parent_joint):
        raise NotImplementedError()


class World(object):
    # General ----------------------------------------------------------------------------------------------------------

    def soft_reset(self):
        """
        keeps robot and other important objects like ground plane
        """
        self.remove_all_objects()
        if self._robot is not None:
            self._robot.reset()

    def hard_reset(self):
        """
        removes everything
        """
        self.soft_reset()
        self.remove_robot()

    def check_collisions(self, cut_off_distances, collision_list_size=20):
        pass

    # Objects ----------------------------------------------------------------------------------------------------------

    def add_object(self, object_):
        """
        :type object_: URDFObject
        """
        # FIXME this interface seems unintuitive, why not pass base pose as well?
        if self.has_robot() and self.robot.get_name() == object_.get_name():
            raise DuplicateNameException(u'object and robot have the same name')
        if self.has_object(object_.get_name()):
            raise DuplicateNameException(u'object with that name already exists')
        self._objects[object_.get_name()] = object_
        logging.loginfo(u'--> added {} to world'.format(object_.get_name()))

    def set_object_pose(self, name, pose):
        """
        :type pose: Pose
        :return:
        """
        self.get_object(name).base_pose = pose

    def get_object(self, name):
        """
        :type name: Union[str, unicode]
        :rtype: WorldObject
        """
        return self._objects[name]

    def get_objects(self):
        return self._objects

    def get_object_names(self):
        """
        :rtype: list
        """
        return list(self._objects.keys())

    def has_object(self, name):
        """
        Checks for objects with the same name.
        :type name: Union[str, unicode]
        :rtype: bool
        """
        return name in self.get_objects()

    def set_object_joint_state(self, name, joint_state):
        """
        :type name: Union[str, unicode]
        :param joint_state: joint name -> SingleJointState
        :type joint_state: dict
        """
        self.get_object(name).joint_state = joint_state

    def remove_object(self, name):
        if self.has_object(name):
            self._objects[name].suicide()
            logging.loginfo(u'<-- removed object {} from world'.format(name))
            del (self._objects[name])
        else:
            raise UnknownBodyException(u'can\'t remove object \'{}\', because it doesn\' exist'.format(name))

    def remove_all_objects(self):
        for object_name in self._objects.keys():
            # I'm not using remove object, because has object ignores hidden objects in pybullet world
            self._objects[object_name].suicide()
            logging.loginfo(u'<-- removed object {} from world'.format(object_name))
        self._objects = {}

    # Robot ------------------------------------------------------------------------------------------------------------

    @profile
    def add_robot(self, robot, base_pose, controlled_joints, ignored_pairs, added_pairs):
        """
        :type robot: giskardpy.world_object.WorldObject
        :type controlled_joints: list
        :type base_pose: Pose
        """
        if not isinstance(robot, WorldObject):
            raise TypeError(u'only WorldObject can be added to world')
        if self.has_robot():
            raise RobotExistsException(u'A robot is already loaded')
        if self.has_object(robot.get_name()):
            raise DuplicateNameException(
                u'can\'t add robot; object with name "{}" already exists'.format(robot.get_name()))
        if base_pose is None:
            base_pose = robot.base_pose
        self._robot = Robot.from_urdf_object(urdf_object=robot,
                                             base_pose=base_pose,
                                             controlled_joints=controlled_joints,
                                             path_to_data_folder=self._path_to_data_folder,
                                             ignored_pairs=ignored_pairs,
                                             added_pairs=added_pairs)
        logging.loginfo(u'--> added {} to world'.format(robot.get_name()))

    @property
    def robot(self):
        """
        :rtype: Robot
        """
        return self._robot

    def has_robot(self):
        """
        :rtype: bool
        """
        return self._robot is not None

    def set_robot_joint_state(self, joint_state):
        """
        Set the current joint state readings for a robot in the world.
        :param joint_state: joint name -> SingleJointState
        :type joint_state: dict
        """
        self._robot.joint_state = joint_state

    def remove_robot(self):
        self._robot = None

    def attach_existing_obj_to_robot(self, name, link, pose):
        """
        :param name: name of the existing object
        :type name: name
        """
        # TODO this should know the object pose and not require it as input
        self._robot.attach_urdf_object(self.get_object(name), link, pose)
        self.remove_object(name)
        logging.loginfo(u'--> attached object {} on link {}'.format(name, link))

    def detach(self, joint_name, from_obj=None):
        if joint_name not in self.robot.get_joint_names():
            raise UnknownBodyException(u'can\'t detach: {}'.format(joint_name))
        if from_obj is None or self.robot.get_name() == from_obj:
            # this only works because attached simple objects have joint names equal to their name
            p = self.robot.get_fk_pose(self.robot.get_root(), joint_name)
            p_map = kdl_to_pose(self.robot.root_T_map.Inverse() * msg_to_kdl(p))

            parent_link = self.robot.get_parent_link_of_joint(joint_name)
            cut_off_obj = self.robot.detach_sub_tree(joint_name)
            logging.loginfo(u'<-- detached {} from link {}'.format(joint_name, parent_link))
        else:
            raise UnsupportedOptionException(u'only detach from robot supported')
        wo = WorldObject.from_urdf_object(cut_off_obj)  # type: WorldObject
        wo.base_pose = p_map
        self.add_object(wo)

    def get_robot_collision_matrix(self, min_dist):
        robot_name = self.robot.get_name()
        collision_matrix = self.robot.get_self_collision_matrix()
        collision_matrix2 = {}
        for link1, link2 in collision_matrix:
            # FIXME should I use the minimum of both distances?
            if self.robot.link_order(link1, link2):
                collision_matrix2[link1, robot_name, link2] = min_dist[link1]
            else:
                collision_matrix2[link2, robot_name, link1] = min_dist[link1]
        return collision_matrix2

    def collision_goals_to_collision_matrix(self, collision_goals, min_dist):
        """
        :param collision_goals: list of CollisionEntry
        :type collision_goals: list
        :return: dict mapping (robot_link, body_b, link_b) -> min allowed distance
        :rtype: dict
        """
        collision_goals = self.verify_collision_entries(collision_goals)
        min_allowed_distance = {}
        for collision_entry in collision_goals:  # type: CollisionEntry
            if self.is_avoid_all_self_collision(collision_entry):
                min_allowed_distance.update(self.get_robot_collision_matrix(min_dist))
                continue
            assert len(collision_entry.robot_links) == 1
            assert len(collision_entry.link_bs) == 1
            key = (collision_entry.robot_links[0], collision_entry.body_b, collision_entry.link_bs[0])
            r_key = (collision_entry.link_bs[0], collision_entry.body_b, collision_entry.robot_links[0])
            if self.is_allow_collision(collision_entry):
                if self.all_link_bs(collision_entry):
                    for key2 in list(min_allowed_distance.keys()):
                        if key[0] == key2[0] and key[1] == key2[1]:
                            del min_allowed_distance[key2]
                elif key in min_allowed_distance:
                    del min_allowed_distance[key]
                elif r_key in min_allowed_distance:
                    del min_allowed_distance[r_key]

            elif self.is_avoid_collision(collision_entry):
                min_allowed_distance[key] = min_dist[key[0]]
            else:
                raise Exception('todo')
        return min_allowed_distance

    def verify_collision_entries(self, collision_goals):
        for ce in collision_goals:  # type: CollisionEntry
            if ce.type in [CollisionEntry.ALLOW_ALL_COLLISIONS,
                           CollisionEntry.AVOID_ALL_COLLISIONS]:
                # logging.logwarn(u'ALLOW_ALL_COLLISIONS and AVOID_ALL_COLLISIONS deprecated, use AVOID_COLLISIONS and'
                #               u'ALLOW_COLLISIONS instead with ALL constant instead.')
                if ce.type == CollisionEntry.ALLOW_ALL_COLLISIONS:
                    ce.type = CollisionEntry.ALLOW_COLLISION
                else:
                    ce.type = CollisionEntry.AVOID_COLLISION

        for ce in collision_goals:  # type: CollisionEntry
            if CollisionEntry.ALL in ce.robot_links and len(ce.robot_links) != 1:
                raise PhysicsWorldException(u'ALL used in robot_links, but it\'s not the only entry')
            if CollisionEntry.ALL in ce.link_bs and len(ce.link_bs) != 1:
                raise PhysicsWorldException(u'ALL used in link_bs, but it\'s not the only entry')
            if ce.body_b == CollisionEntry.ALL and not self.all_link_bs(ce):
                raise PhysicsWorldException(u'if body_b == ALL, link_bs has to be ALL as well')

        self.are_entries_known(collision_goals)

        for ce in collision_goals:
            if not ce.robot_links:
                ce.robot_links = [CollisionEntry.ALL]
            if not ce.link_bs:
                ce.link_bs = [CollisionEntry.ALL]

        for i, ce in enumerate(reversed(collision_goals)):
            if self.is_avoid_all_collision(ce):
                collision_goals = collision_goals[len(collision_goals) - i - 1:]
                break
            if self.is_allow_all_collision(ce):
                collision_goals = collision_goals[len(collision_goals) - i:]
                break
        else:
            ce = CollisionEntry()
            ce.type = CollisionEntry.AVOID_COLLISION
            ce.robot_links = [CollisionEntry.ALL]
            ce.body_b = CollisionEntry.ALL
            ce.link_bs = [CollisionEntry.ALL]
            ce.min_dist = -1
            collision_goals.insert(0, ce)

        # split body bs
        collision_goals = self.split_body_b(collision_goals)

        # split robot links
        collision_goals = self.robot_related_stuff(collision_goals)

        # split link_bs
        collision_goals = self.split_link_bs(collision_goals)

        return collision_goals

    def are_entries_known(self, collision_goals):
        robot_name = self.robot.get_name()
        robot_links = set(self.robot.get_link_names())
        for collision_entry in collision_goals:
            if not (collision_entry.body_b == robot_name or
                    collision_entry.body_b in self.get_object_names() or
                    self.all_body_bs(collision_entry)):
                raise UnknownBodyException(u'body b \'{}\' unknown'.format(collision_entry.body_b))
            if not self.all_robot_links(collision_entry):
                for robot_link in collision_entry.robot_links:
                    if robot_link not in robot_links:
                        raise UnknownBodyException(u'robot link \'{}\' unknown'.format(robot_link))
            if collision_entry.body_b == robot_name:
                for robot_link in collision_entry.link_bs:
                    if robot_link != CollisionEntry.ALL and robot_link not in robot_links:
                        raise UnknownBodyException(
                            u'link b \'{}\' of body \'{}\' unknown'.format(robot_link, collision_entry.body_b))
            elif not self.all_body_bs(collision_entry) and not self.all_link_bs(collision_entry):
                object_links = self.get_object(collision_entry.body_b).get_link_names()
                for link_b in collision_entry.link_bs:
                    if link_b not in object_links:
                        raise UnknownBodyException(
                            u'link b \'{}\' of body \'{}\' unknown'.format(link_b, collision_entry.body_b))

    def split_link_bs(self, collision_goals):
        # FIXME remove the side effects of these three methods
        i = 0
        while i < len(collision_goals):
            collision_entry = collision_goals[i]
            if self.is_avoid_all_self_collision(collision_entry):
                i += 1
                continue
            if self.all_link_bs(collision_entry):
                if collision_entry.body_b == self.robot.get_name():
                    new_ces = []
                    link_bs = self.robot.get_possible_collisions(list(collision_entry.robot_links)[0])
                elif [x for x in collision_goals[i:] if
                      x.robot_links == collision_entry.robot_links and
                      x.body_b == collision_entry.body_b and not self.all_link_bs(x)]:
                    new_ces = []
                    link_bs = self.get_object(collision_entry.body_b).get_link_names_with_collision()
                else:
                    i += 1
                    continue
                collision_goals.remove(collision_entry)
                for link_b in link_bs:
                    ce = CollisionEntry()
                    ce.type = collision_entry.type
                    ce.robot_links = collision_entry.robot_links
                    ce.body_b = collision_entry.body_b
                    ce.min_dist = collision_entry.min_dist
                    ce.link_bs = [link_b]
                    new_ces.append(ce)
                for new_ce in new_ces:
                    collision_goals.insert(i, new_ce)
                i += len(new_ces)
                continue
            elif len(collision_entry.link_bs) > 1:
                collision_goals.remove(collision_entry)
                for link_b in collision_entry.link_bs:
                    ce = CollisionEntry()
                    ce.type = collision_entry.type
                    ce.robot_links = collision_entry.robot_links
                    ce.body_b = collision_entry.body_b
                    ce.link_bs = [link_b]
                    ce.min_dist = collision_entry.min_dist
                    collision_goals.insert(i, ce)
                i += len(collision_entry.link_bs)
                continue
            i += 1
        return collision_goals

    def robot_related_stuff(self, collision_goals):
        i = 0
        controlled_robot_links = self.robot.get_controlled_links()
        while i < len(collision_goals):
            collision_entry = collision_goals[i]
            if self.is_avoid_all_self_collision(collision_entry):
                i += 1
                continue
            if self.all_robot_links(collision_entry):
                collision_goals.remove(collision_entry)

                new_ces = []
                for robot_link in controlled_robot_links:
                    ce = CollisionEntry()
                    ce.type = collision_entry.type
                    ce.robot_links = [robot_link]
                    ce.body_b = collision_entry.body_b
                    ce.min_dist = collision_entry.min_dist
                    ce.link_bs = collision_entry.link_bs
                    new_ces.append(ce)

                for new_ce in new_ces:
                    collision_goals.insert(i, new_ce)
                i += len(new_ces)
                continue
            elif len(collision_entry.robot_links) > 1:
                collision_goals.remove(collision_entry)
                for robot_link in collision_entry.robot_links:
                    ce = CollisionEntry()
                    ce.type = collision_entry.type
                    ce.robot_links = [robot_link]
                    ce.body_b = collision_entry.body_b
                    ce.min_dist = collision_entry.min_dist
                    ce.link_bs = collision_entry.link_bs
                    collision_goals.insert(i, ce)
                i += len(collision_entry.robot_links)
                continue
            i += 1
        return collision_goals

    def split_body_b(self, collision_goals):
        i = 0
        while i < len(collision_goals):
            collision_entry = collision_goals[i]
            if self.all_body_bs(collision_entry):
                collision_goals.remove(collision_entry)
                new_ces = []
                for body_b in [self.robot.get_name()] + self.get_object_names():
                    ce = CollisionEntry()
                    ce.type = collision_entry.type
                    ce.robot_links = collision_entry.robot_links
                    ce.min_dist = collision_entry.min_dist
                    ce.body_b = body_b
                    ce.link_bs = collision_entry.link_bs
                    new_ces.append(ce)
                for new_ce in reversed(new_ces):
                    collision_goals.insert(i, new_ce)
                i += len(new_ces)
                continue
            i += 1
        return collision_goals

    def all_robot_links(self, collision_entry):
        return CollisionEntry.ALL in collision_entry.robot_links and len(collision_entry.robot_links) == 1

    def all_link_bs(self, collision_entry):
        return CollisionEntry.ALL in collision_entry.link_bs and len(collision_entry.link_bs) == 1 or \
               not collision_entry.link_bs

    def all_body_bs(self, collision_entry):
        return collision_entry.body_b == CollisionEntry.ALL

    def is_avoid_collision(self, collision_entry):
        return collision_entry.type in [CollisionEntry.AVOID_COLLISION, CollisionEntry.AVOID_ALL_COLLISIONS]

    def is_allow_collision(self, collision_entry):
        return collision_entry.type in [CollisionEntry.ALLOW_COLLISION, CollisionEntry.ALLOW_ALL_COLLISIONS]

    def is_avoid_all_self_collision(self, collision_entry):
        """
        :type collision_entry: CollisionEntry
        :return: bool
        """
        return self.is_avoid_collision(collision_entry) \
               and self.all_robot_links(collision_entry) \
               and collision_entry.body_b == self.robot.get_name() \
               and self.all_link_bs(collision_entry)

    def is_allow_all_self_collision(self, collision_entry):
        """
        :type collision_entry: CollisionEntry
        :return: bool
        """
        return self.is_allow_collision(collision_entry) \
               and self.all_robot_links(collision_entry) \
               and collision_entry.body_b == self.robot.get_name() \
               and self.all_link_bs(collision_entry)

    def is_avoid_all_collision(self, collision_entry):
        """
        :type collision_entry: CollisionEntry
        :return: bool
        """
        return self.is_avoid_collision(collision_entry) \
               and self.all_robot_links(collision_entry) \
               and self.all_body_bs(collision_entry) \
               and self.all_link_bs(collision_entry)

    def is_allow_all_collision(self, collision_entry):
        """
        :type collision_entry: CollisionEntry
        :return: bool
        """
        return self.is_allow_collision(collision_entry) \
               and self.all_robot_links(collision_entry) \
               and self.all_body_bs(collision_entry) \
               and self.all_link_bs(collision_entry)
