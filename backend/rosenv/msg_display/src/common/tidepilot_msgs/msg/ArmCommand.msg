std_msgs/Header header

# 0代表什么都不操作 1代表停  2代表动
int32 action_vertical
int32 action_horizontal
#推杆操作 0代表推杆暂停 1代表收 2代表推
int32 action_pushrod

#向上是正 向下是负
float32 vertical_dis
#是否是绝对距离 false 是相对距离  true是绝对距离
bool vertical_absolute

#向正是正 向右是负
float32 horizontal_dis
bool horizontal_absolute

#1 只有机械臂回到原点 丝杆不动
#2 机械臂 丝杆都回到原点
int32 back2origin
