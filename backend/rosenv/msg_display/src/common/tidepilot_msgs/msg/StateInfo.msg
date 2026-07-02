uint8 MANUAL=0
uint8 AUTO  =1
uint8 REMOTE=2
bool throttle_pedal_state
bool brake_pedal_state
bool gear_shifter_state
bool steering_wheel_state
bool gear_mode
bool mode_button
bool emergency_stop
bool collision_stop
bool steering_malfunction
bool steering_release
int32 vehicle_ready_status
int32 vehicle_error_level 
bool high_beam_signal 
bool low_beam_signal 
bool left_turn_signal 
bool right_turn_signal 
bool danger_alarm_lamp_signal 
bool clearance_lamp_signal 
bool reversing_lamp_signal
bool foglight_signal 
bool door_signal 
bool paking_brake_signal 
bool cab_switch_signal 
float64 battery_available
float64 battery_soh
string battery_id
int32 charge_status
int32 motor_temperature 
int32 motor_busbar_voltage 
float64 speed_kmph 
uint8 gear_position 
uint8 truck_control_mode 
int32 gear_position_num
int32 system_faultcode
bool gear_in_shifting_state
int32 motor_torque_report
bool ultrasonic_safetylock_ctrl_sts
bool lidar_safetylock_ctrl_sts
bool left_park_sw_sts
bool right_park_sw_sts
bool hand_rc_estop_ctrl_sts
bool net_rc_estop_ctrl_sts
int32 hand_rc_faultcode
int32 net_rc_faultcode
int32 vehicle_id
int32 vehicle_miles_traveled
bool hydraulic_brake_sts
float64 motor1_torquereport
float64 motor2_torquereport
float64 motor1_speedreport
float64 motor2_speedreport
float64 axle1_leftwheelangle
float64 axle1_rightwheelangle
float64 axle2_leftwheelangle
float64 axle2_rightwheelangle
float64 axle3_leftwheelangle
float64 axle3_rightwheelangle
float64 axle4_leftwheelangle
float64 axle4_rightwheelangle
bool power_ready
bool throttle_enable
bool brake_enable
bool gear_enable
bool steer_enable
bool bcm_enable
bool chassis_ready
int32 throttle_pos
int32 brake_pos
int32 battary_swap_sts
int32 on_swap_station_sts
bool reverse_radar_sts
bool horn_signal
bool front_daytime_runlamp_signal
bool rear_daytime_runlamp_signal
float64 fl_tire_pressure  #前左胎压
float64 fr_tire_pressure   #前右胎压
float64 rli_tire_pressure   #后左里胎压
float64 rlo_tire_pressure   #后左外胎压
float64 rri_tire_pressure   #后右里胎压
float64 rro_tire_pressure   #后右外胎压
int32 tire_high_temperature_sts
int32 tire_leakage_sts
int32 tire_pressure_sts
float64 park_press
float64 front_press
float64 rear_press
int32 chassis_version
int32 input_temperature  #设备舱温度
int32 tm2b_inlet_temp  #电池冷却液温度
float64 a1_lro_tire_pressure #挂1轴左后外胎压
float64 a1_lri_tire_pressure #挂1轴左后内胎压
float64 a1_rri_tire_pressure #挂1轴右后内胎压
float64 a1_rro_tire_pressure #挂1轴右后外胎压
float64 a2_lro_tire_pressure #挂2轴左后外胎压
float64 a2_lri_tire_pressure #挂2轴左后内胎压
float64 a2_rri_tire_pressure #挂2轴右后内胎压
float64 a2_rro_tire_pressure #挂2轴右后外胎压
float64 fl_tire_temperature  #前左胎压
float64 fr_tire_temperature   #前右胎压
float64 rlo_tire_temperature   #后左外胎压
float64 rli_tire_temperature   #后左里胎压
float64 rri_tire_temperature   #后右里胎压
float64 rro_tire_temperature   #后右外胎压
float64 battery_temperature   #电池温度
float64 output_current  #电池电流