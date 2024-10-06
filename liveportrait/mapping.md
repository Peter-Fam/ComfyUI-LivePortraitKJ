# First implementation (live_portrait_pipeline.py) -> Second implementation (live_portrait_pipeline.py in document 3)

# Input data
driving_images -> driving_rgb_lst
crop_info -> ret_d (for driving video), ret_s (for source video)
driving_landmarks -> driving_lmk_crop_lst

# Configuration
inference_cfg -> inference_cfg (same name, but likely different structure)
device -> device (same)

# Output data
out_list -> out_list (same)

# Keypoint and motion information
x_d_info -> x_d_i_info
x_s_info -> x_s_info (same)
R_d -> R_d_i
R_s -> R_s (same)

# Feature extraction
f_s -> f_s (same)
x_s -> x_s (same)

# Motion smoothing
x_d_r_lst_smooth -> driving_rot_list_smooth
x_d_exp_lst_smooth -> driving_exp_list_smooth

# Retargeting
combined_eye_ratio_tensor -> combined_eye_ratio_tensor (same)
combined_lip_ratio_tensor -> combined_lip_ratio_tensor (same)

# Animation parameters
delta_multiplier -> inf_cfg.driving_multiplier
relative_motion_mode -> inf_cfg.flag_relative_motion
driving_smooth_observation_variance -> inf_cfg.driving_smooth_observation_variance

# Output processing
I_p_i -> I_p_i (same)
I_p_lst -> I_p_lst (same)
I_p_pstbk -> I_p_pstbk (same)
I_p_pstbk_lst -> I_p_pstbk_lst (same)

# File handling (only in second implementation)
# N/A -> wfp, wfp_concat, wfp_template

# Audio handling (only in second implementation)
# N/A -> add_audio_to_video(), has_audio_stream()