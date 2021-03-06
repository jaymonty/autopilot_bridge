#!/usr/bin/env python

#-----------------------------------------------------------------------
# ROS-MAVLink Bridge node
# Main program and a small set of standard pubs/subs
#
# Originally developed by Mike Clement, 2014
#
# This software may be freely used, modified, and distributed.
# This software comes with no warranty.

#-----------------------------------------------------------------------
# Import a bunch of libraries

# Standard Python imports
from argparse import ArgumentParser
import os
import sys

# Import "standard" ROS message types
from sensor_msgs.msg import Imu, NavSatFix, NavSatStatus
from nav_msgs.msg import Odometry
from geometry_msgs.msg import Quaternion
import std_msgs.msg as stdmsg
import autopilot_bridge.msg as apmsg

# Other needed ROS imports
import rospy
from tf.transformations import quaternion_from_euler

# Import the bridge
from pymavlink import mavutil
from MAVLinkBridge import MAVLinkBridge

#-----------------------------------------------------------------------
# Standard ROS subscribers

# Purpose: (Dis)arm throttle
# Fields: 
# .data - True arms, False disarms
def sub_arm_throttle(message, bridge):
    if message.data:
        bridge.get_master().arducopter_arm()
    else:
        bridge.get_master().arducopter_disarm()

# Purpose: Changes autopilot mode
# (must be in mode_mapping dictionary)
# Fields: 
# .data - string representing mode
def sub_change_mode(message, bridge):
    mav_map = bridge.get_master().mode_mapping()
    if message.data in mav_map:
        bridge.get_master().set_mode(mav_map[message.data])
    else:
        raise Exception("invalid mode " + message.data)

# Purpose: Go to a lat/lon/alt in GUIDED mode
# Fields: 
# .lat - Decimal degrees
# .lon - Decimal degrees
# .alt - Decimal meters **AGL wrt home**
def sub_guided_goto(message, bridge):
    bridge.get_master().mav.mission_item_send(
        bridge.get_master().target_system,
        bridge.get_master().target_component,
        0,
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
        mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
        2, 0, 0, 0, 0, 0,
        message.lat, message.lon, message.alt)

# Purpose: Go to a specified waypoint by index/sequence number
# Fields: 
# .data - Must be an index in current mission set
def sub_waypoint_goto(message, bridge):
    bridge.get_master().waypoint_set_current_send(message.data)

#-----------------------------------------------------------------------
# Standard ROS publishers

# Publish an Imu message
def pub_imu(msg_type, msg, bridge):
    pub = bridge.get_ros_pub("imu", Imu, queue_size=1)
    imu = Imu()
    imu.header.stamp = bridge.project_ap_time(msg)
    imu.header.frame_id = 'base_footprint'
    # Orientation as a Quaternion, with unknown covariance
    quat = quaternion_from_euler(msg.roll, msg.pitch, msg.yaw, 'sxyz')
    imu.orientation = Quaternion(quat[0], quat[1], quat[2], quat[3])
    imu.orientation_covariance = (0, 0, 0, 0, 0, 0, 0, 0, 0)
    # Angular velocities, with unknown covariance
    imu.angular_velocity.x = msg.rollspeed
    imu.angular_velocity.y = msg.pitchspeed
    imu.angular_velocity.z = msg.yawspeed
    imu.angular_velocity_covariance = (0, 0, 0, 0, 0, 0, 0, 0, 0)
    # Not supplied with linear accelerations
    imu.linear_acceleration_covariance[0] = -1
    pub.publish(imu)

# Publish GPS data in NavSatFix form
def pub_gps(msg_type, msg, bridge):
    pub = bridge.get_ros_pub("gps", NavSatFix, queue_size=1)
    fix = NavSatFix()
    fix.header.stamp = bridge.project_ap_time(msg)
    fix.header.frame_id = 'base_footprint'
    fix.latitude = msg.lat/1e07
    fix.longitude = msg.lon/1e07
    # NOTE: absolute (MSL) altitude
    fix.altitude = msg.alt/1e03
    fix.status.status = NavSatStatus.STATUS_FIX
    fix.status.service = NavSatStatus.SERVICE_GPS
    fix.position_covariance_type = NavSatFix.COVARIANCE_TYPE_UNKNOWN
    pub.publish(fix)
                
# Publish GPS data in Odometry form
def pub_gps_odom(msg_type, msg, bridge):
    pub = bridge.get_ros_pub("gps_odom", Odometry, queue_size=1)
    odom = Odometry()
    odom.header.stamp = bridge.project_ap_time(msg)
    odom.header.frame_id = 'base_footprint'
    odom.pose.pose.position.x = msg.lat/1e07
    odom.pose.pose.position.y = msg.lon/1e07
    # NOTE: relative (AGL wrt takeoff point) altitude
    odom.pose.pose.position.z = msg.relative_alt/1e03
    # An identity orientation since GPS doesn't provide us with one
    odom.pose.pose.orientation.x = 1
    odom.pose.pose.orientation.y = 0
    odom.pose.pose.orientation.z = 0
    odom.pose.pose.orientation.w = 0
    # robot_pose_ekf docs suggested using this for covariance
    odom.pose.covariance = ( 0.1, 0, 0, 0, 0, 0,
                             0, 0.1, 0, 0, 0, 0,
                             0, 0, 0.1, 0, 0, 0,
                             0, 0, 0, 99999, 0, 0,
                             0, 0, 0, 0, 99999, 0,
                             0, 0, 0, 0, 0, 99999 )
    # Linear velocities, with unknown covariance
    odom.twist.twist.linear.x = msg.vx / 1e02
    odom.twist.twist.linear.y = msg.vy / 1e02
    odom.twist.twist.linear.z = msg.vz / 1e02
    odom.twist.covariance = odom.pose.covariance
    pub.publish(odom)

#-----------------------------------------------------------------------
# Start-up

if __name__ == '__main__':
    # Grok args
    # TODO: Make these ROS params
    parser = ArgumentParser("rosrun autopilot_bridge mavbridge.py")
    parser.add_argument('-d', "--device", dest="device", 
                        help="serial device or network socket", default=None)
    parser.add_argument('-b', "--baudrate", dest="baudrate", type=int,
                        help="serial baud rate", default=57600)
    parser.add_argument('-m', "--module", dest="module", action='append', 
                        help="load module mavbridge_MODULE.pyc")
    parser.add_argument("--looprate", dest="looprate",
                        help="Rate at which internal loop runs", default=50)
    parser.add_argument("--ros-basename", dest="basename", 
                        help="ROS namespace basename", default="autopilot")
    parser.add_argument("--gps-time-hack", dest="gps_time_hack", 
                        action="store_true", default=False,
                        help="try to set system clock from autopilot time")
    parser.add_argument("--serial-relief", default=0, type=int, dest="serial_relief",
                        help="limit serial backlog to N bytes (0=disabled)")
    parser.add_argument("--spam-mavlink", dest="spam_mavlink", 
                        action="store_true", default=False,
                        help="print every received mavlink message")
    args = parser.parse_args(args=rospy.myargv(argv=sys.argv)[1:])

    # Handle special device cases
    if args.device and args.device.find(':') == -1:
        # If specifying a device and not a network connection
        if ',' in args.device:
            # Allow "device,baudrate" syntax for convenience
            args.device, args.baudrate = args.device.split(',')

    # User-friendly hello message
    dev = args.device
    if args.device is None: dev = "auto-detect"
    print "Starting MAVLink <-> ROS interface over the following link:\n" + \
          ("  device:\t\t%s\n" % dev) + \
          ("  baudrate:\t\t%s\n" % str(args.baudrate))


    # Initialize the bridge
    try:
        bridge = MAVLinkBridge(device=args.device,
                               baudrate=args.baudrate,
                               basename=args.basename,
                               loop_rate=args.looprate,
                               sync_local_clock=args.gps_time_hack,
                               serial_relief=args.serial_relief,
                               spam_mavlink=args.spam_mavlink)
    except Exception as ex:
        print str(ex)
        sys.exit(-1)

    # Register Standard MAVLink events
    bridge.add_mavlink_event("ATTITUDE", pub_imu)
    bridge.add_mavlink_event("GLOBAL_POSITION_INT", pub_gps)
    bridge.add_mavlink_event("GLOBAL_POSITION_INT", pub_gps_odom)

    # Register Standard ROS Subcriber events
    bridge.add_ros_sub_event("arm", stdmsg.Bool, sub_arm_throttle) 
    bridge.add_ros_sub_event("mode", stdmsg.String, sub_change_mode) 
    bridge.add_ros_sub_event("guided_goto", apmsg.LLA, sub_guided_goto) 
    bridge.add_ros_sub_event("waypoint_goto", stdmsg.UInt16, sub_waypoint_goto) 

    # Register modules
    if args.module is not None:
        for m in args.module:
            m_name = "mavbridge_%s" % m
            if bridge.get_module(m_name) is not None:
                print "Module '%s' already loaded" % m_name
                continue
            try:
                # Blatantly copied from MAVProxy and a Stack Overflow answer;
                # necessary to deal with subtleties of Python imports
                m_obj = __import__(m_name)
                components = m_name.split('.')
                for comp in components[1:]:
                    m_obj = getattr(m_obj, comp)
                reload(m_obj)
                bridge.add_module(m, m_obj)
                print "Module '%s' loaded successfully" % m_name
            except Exception as ex:
                print "Failed to load module '%s': %s" % (m_name, str(ex))

    # Start loop (this won't return until we terminate or ROS shuts down)
    print "Starting autopilot bridge loop...\n"
    while True:
        # TODO: restart within run_loop() should obviate this loop
        try:
            bridge.run_loop()
            break  # If loop legitimately returned, we'll exit
        except:
            print "... unhandled MAVLinkBridge error, restarting loop ..."

