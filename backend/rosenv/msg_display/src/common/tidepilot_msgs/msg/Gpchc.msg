# Message from GPCHC NMEA String
std_msgs/Header header

string message_id

# UTC seconds from midnight
float64 utc_seconds

uint32 gps_week
float64 gps_time

float32 heading
float32 pitch
float32 roll

float32 gyro_x
float32 gyro_y
float32 gyro_z

float32 acc_x
float32 acc_y
float32 acc_z

float64 lat
float64 lon
float64 alt

float32 vel_east
float32 vel_north
float32 vel_up
float32 speed

uint32 num_sats_master
uint32 num_sats_slave

uint32 status
uint32 ins_status
uint32 diff_age
