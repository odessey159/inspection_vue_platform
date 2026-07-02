std_msgs/Header header

string vehicle_id
float32 x
float32 y
float32 yaw
float32 tx
float32 ty
float32 tyaw
float32 speed
#车辆加速度
float32 accel
#车轮角度
float32 front_steer
float32 back_steer

#车辆坐标系下托挂角度
float32 trailer_angle

#1 车辆在动  0车辆静止
bool motion_status
int32 traj_index

