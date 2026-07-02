std_msgs/Header header

int32 task_type
int32 area #1原点 2电梯外部按钮  3电梯内部按钮  4住户门口
float32 dest_x
float32 dest_y
float32 dest_heading
int32 floor #目标楼层
int32 direction 
geometry_msgs/Pose2D[] via_points
