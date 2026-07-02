std_msgs/Header header

#1 按钮被按下
#2 电梯到达
int32 status

#如果电梯到达 是去哪部电梯
#0是左边电梯 1是右边电梯 左右按面向电梯的情况计算
int32 target_elevator