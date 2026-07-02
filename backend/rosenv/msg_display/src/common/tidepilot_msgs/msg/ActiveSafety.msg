std_msgs/Header header

# 0 estop 1 正常刹车 2 慢慢减速到0 3 限速
uint8 status
float32 speed_limit
float32 current_speed
string module
int32 control_mode
int32 collision_mode