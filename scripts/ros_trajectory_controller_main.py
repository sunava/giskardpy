import rospy

from giskardpy.plugin_action_server import ActionServer
from giskardpy.application import ROSApplication
from giskardpy.plugin_instantaneous_controller import JointControllerPlugin, CartesianControllerPlugin
from giskardpy.plugin_fk import FKPlugin
from giskardpy.plugin_joint_state import JointStatePlugin
from giskardpy.process_manager import ProcessManager

if __name__ == '__main__':
    rospy.init_node('muh')

    process_manager = ProcessManager()
    process_manager.register_plugin('js', JointStatePlugin())
    process_manager.register_plugin('fk', FKPlugin('base_footprint', 'gripper_tool_frame'))
    process_manager.register_plugin('action server', ActionServer())
    process_manager.register_plugin('joint controller', JointControllerPlugin())
    # TODO prevent controller from overwriting motor commands from other controllers
    process_manager.register_plugin('cartesian controller', CartesianControllerPlugin())

    app = ROSApplication(process_manager)
    app.run()
