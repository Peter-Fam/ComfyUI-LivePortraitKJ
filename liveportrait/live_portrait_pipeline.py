# coding: utf-8

"""
Pipeline of LivePortrait
"""

import comfy.utils
import comfy.model_management as mm
import gc
import pprint
from tqdm import tqdm
import numpy as np
from .config.inference_config import InferenceConfig
from .utils.camera import get_rotation_matrix
from .live_portrait_wrapper import LivePortraitWrapper
from .utils.retargeting_utils import calc_eye_close_ratio, calc_lip_close_ratio
from .utils.filter import smooth
from .utils.helper import calc_motion_multiplier
import os.path as osp
import pickle as pkl
import torch


def make_abs_path(fn):
    return osp.join(osp.dirname(osp.realpath(__file__)), fn)
import os
script_directory = os.path.dirname(os.path.abspath(__file__))
def load_lip_array():
    with open(make_abs_path('./utils/resources/lip_array.pkl'), 'rb') as f:
        return pkl.load(f)
class LivePortraitPipeline(object):
    def __init__(
        self,
        appearance_feature_extractor,
        motion_extractor,
        warping_module,
        spade_generator,
        stitching_retargeting_module,
        inference_cfg: InferenceConfig,
    ):
        self.live_portrait_wrapper: LivePortraitWrapper = LivePortraitWrapper(
            appearance_feature_extractor,
            motion_extractor,
            warping_module,
            spade_generator,
            stitching_retargeting_module,
            cfg=inference_cfg,
        )

    def execute(
        self, driving_images, crop_info, driving_landmarks, delta_multiplier, relative_motion_mode, driving_smooth_observation_variance, mismatch_method="constant", expression_friendly=False, driving_multiplier=1.0, 
    ):
        inference_cfg = self.live_portrait_wrapper.cfg
        device = inference_cfg.device_id

        out_list = []
        R_d_0, x_d_0_info = None, None

        source_images_num = len(crop_info["crop_info_list"])

        if mismatch_method == "cut" or relative_motion_mode == "source_video_smoothed":
            total_frames = source_images_num
        else:
            total_frames = driving_images.shape[0]


        disable_progress_bar = True if relative_motion_mode == "single_frame" else False

        source_info = crop_info["source_info"]
        source_rot_list = crop_info["source_rot_list"]
        f_s_list = crop_info["f_s_list"]
        x_s_list = crop_info["x_s_list"]

        driving_info = []
        driving_exp_list = []
        driving_rot_list = []
        
        for i in tqdm(range(driving_images.shape[0]), desc='Processing driving images...', total=driving_images.shape[0], disable=disable_progress_bar):
            #get driving keypoints info
            safe_index = min(i, source_images_num - 1)
            if crop_info["crop_info_list"][safe_index] is None:
                driving_info.append(None)
                driving_rot_list.append(None)
                driving_exp_list.append(None)
                if i == 0:
                    raise ValueError("No face detected in FIRST source image")
                continue
            x_d_info = self.live_portrait_wrapper.get_kp_info(driving_images[i].unsqueeze(0).to(device))
            
            if i == 0:
                first = x_d_info

            driving_info.append(x_d_info)

            driving_exp = source_info[safe_index]["exp"] + x_d_info["exp"] - first["exp"]
            driving_exp_list.append(driving_exp.cpu())

            R_d = get_rotation_matrix(
                x_d_info["pitch"], x_d_info["yaw"], x_d_info["roll"]
            )
            driving_rot_list.append(R_d)

        if relative_motion_mode == "source_video_smoothed" or relative_motion_mode == "expression_only":
            x_d_r_lst = []
            first_driving_rot = driving_rot_list[0].cpu().numpy().astype(np.float32).transpose(0, 2, 1)
            for i in tqdm(range(source_images_num), desc='Smoothing...', total=source_images_num):
                if driving_rot_list[i] is None:
                    x_d_r_lst.append(None)
                    continue
                driving_rot = driving_rot_list[i].cpu().numpy().astype(np.float32)
                source_rot = source_rot_list[i].cpu().numpy().astype(np.float32)
                dot = np.dot(driving_rot, first_driving_rot) @ source_rot
                x_d_r_lst.append(dot)
  
            driving_exp_list_smooth = smooth(driving_exp_list, source_info[0]["exp"].shape, device, observation_variance=driving_smooth_observation_variance)
            driving_rot_list_smooth = smooth(x_d_r_lst, source_rot_list[0].shape, device, observation_variance=driving_smooth_observation_variance)

        pbar = comfy.utils.ProgressBar(total_frames)

        for i in tqdm(range(total_frames), desc='Animating...', total=total_frames, disable=disable_progress_bar):

            safe_index = min(i, len(crop_info["crop_info_list"]) - 1)

            # skip and return empty frames if no crop due to no face detected
            if crop_info["crop_info_list"][safe_index] is None:
                out_list.append({})
                pbar.update(1)
                continue

            source_lmk = crop_info["crop_info_list"][safe_index]["lmk_crop"]
            
            x_d_info = driving_info[i]
            R_d = driving_rot_list[i]

            x_s_info = source_info[safe_index]
            R_s = source_rot_list[safe_index]
            f_s = f_s_list[safe_index]
            x_s = x_s_list[safe_index]

            x_c_s = x_s_info["kp"]

            #lip zero
            if inference_cfg.flag_lip_zero:
                c_d_lip_before_animation = [0.0]
                combined_lip_ratio_tensor_before_animation = (self.live_portrait_wrapper.calc_combined_lip_ratio(c_d_lip_before_animation, source_lmk))

                if (combined_lip_ratio_tensor_before_animation[0][0] < inference_cfg.lip_zero_threshold):
                    inference_cfg.flag_lip_zero = False
                else:
                    lip_delta_before_animation = (self.live_portrait_wrapper.retarget_lip(x_s, combined_lip_ratio_tensor_before_animation))

            if relative_motion_mode == "relative":
                if i == 0:
                    R_d_0 = R_d
                    x_d_0_info = x_d_info
                R_new = (R_d @ R_d_0.permute(0, 2, 1)) @ R_s
                delta_new = x_s_info["exp"] + (x_d_info["exp"] - x_d_0_info["exp"])
                scale_new = x_s_info["scale"] * (x_d_info["scale"] / x_d_0_info["scale"])
                t_new = x_s_info["t"] + (x_d_info["t"] - x_d_0_info["t"])
            elif relative_motion_mode == "source_video_smoothed":
                R_new = driving_rot_list_smooth[i]
                delta_new = driving_exp_list_smooth[i]
                scale_new = x_s_info["scale"]
                t_new = x_d_info["t"]
            elif relative_motion_mode == "expression_only":
                R_new = R_s
                delta_new = x_s_info['exp'].clone();
                # delta_new = driving_exp_list_smooth[i]
                for idx in [1,2,6,11,12,13,14,15,16,17,18,19,20]:
                    delta_new[:, idx, :] = driving_exp_list_smooth[i][idx, :]
                delta_new[:, 3:5, 1] = driving_exp_list_smooth[i][3:5, 1]
                delta_new[:, 5, 2] = driving_exp_list_smooth[i][5, 2]
                delta_new[:, 8, 2] = driving_exp_list_smooth[i][8, 2]
                delta_new[:, 9, 1:] = driving_exp_list_smooth[i][9, 1:]
                scale_new = x_s_info["scale"]
                t_new = x_d_info["t"]
            elif relative_motion_mode == "relative_rotation_only":
                R_new = R_s
                delta_new = x_s_info['exp']
                scale_new = x_s_info["scale"]
                t_new = x_d_info["t"]
            elif relative_motion_mode == "single_frame":
                R_new = R_d
                delta_new = x_d_info['exp']
                scale_new = x_s_info["scale"]
                t_new = x_d_info["t"]
            else:
                R_new = R_d
                delta_new = x_s_info['exp']
                scale_new = x_s_info["scale"]
                t_new = x_d_info["t"]

            t_new[..., 2].fill_(0)  # zero tz

            delta_new = delta_new * delta_multiplier
            
            x_d_i_new = scale_new * (x_c_s @ R_new + delta_new) + t_new

            if expression_friendly:
                if i == 0:
                    x_d_0_new = x_d_i_new
                    motion_multiplier = calc_motion_multiplier(x_s, x_d_0_new)
                    motion_multiplier *= driving_multiplier
                x_d_diff = (x_d_i_new - x_d_0_new) * motion_multiplier
                x_d_i_new = x_d_diff + x_s

            if (
                not inference_cfg.flag_stitching
                and not inference_cfg.flag_eye_retargeting
                and not inference_cfg.flag_lip_retargeting
                ):
                # without stitching or retargeting
                if inference_cfg.flag_lip_zero:
                    x_d_i_new += lip_delta_before_animation.reshape(-1, x_s.shape[1], 3)
                else:
                    pass
            elif (
                inference_cfg.flag_stitching
                and not inference_cfg.flag_eye_retargeting
                and not inference_cfg.flag_lip_retargeting
                ):
                # with stitching and without retargeting
                if inference_cfg.flag_lip_zero:
                    x_d_i_new = self.live_portrait_wrapper.stitching(
                        x_s, x_d_i_new
                    ) + lip_delta_before_animation.reshape(-1, x_s.shape[1], 3)
                else:
                    x_d_i_new = self.live_portrait_wrapper.stitching(x_s, x_d_i_new)

            #with eye/lip retargeting
            else:
                eyes_delta, lip_delta = None, None
                if inference_cfg.flag_eye_retargeting:
                    c_d_eyes_i = calc_eye_close_ratio(driving_landmarks[i][None])
                    combined_eye_ratio_tensor = (
                        self.live_portrait_wrapper.calc_combined_eye_ratio(
                            c_d_eyes_i, source_lmk
                        )
                    )
                    combined_eye_ratio_tensor = (
                        combined_eye_ratio_tensor
                        * inference_cfg.eyes_retargeting_multiplier
                    )
                    # ∆_eyes,i = R_eyes(x_s; c_s,eyes, c_d,eyes,i)
                    eyes_delta = self.live_portrait_wrapper.retarget_eye(
                        x_s, combined_eye_ratio_tensor
                    )
                if inference_cfg.flag_lip_retargeting:
                    c_d_lip_i = calc_lip_close_ratio(driving_landmarks[i][None])
                    combined_lip_ratio_tensor = (
                        self.live_portrait_wrapper.calc_combined_lip_ratio(
                            c_d_lip_i, source_lmk
                        )
                    )
                    combined_lip_ratio_tensor = (
                        combined_lip_ratio_tensor
                        * inference_cfg.lip_retargeting_multiplier
                    )
                    # ∆_lip,i = R_lip(x_s; c_s,lip, c_d,lip,i)
                    lip_delta = self.live_portrait_wrapper.retarget_lip(
                        x_s, combined_lip_ratio_tensor
                    )

                if relative_motion_mode != "off":  # use x_s
                    x_d_i_new = (
                        x_s
                        + (
                            eyes_delta.reshape(-1, x_s.shape[1], 3)
                            if eyes_delta is not None
                            else 0
                        )
                        + (
                            lip_delta.reshape(-1, x_s.shape[1], 3)
                            if lip_delta is not None
                            else 0
                        )
                    )
                else:  # use x_d,i
                    x_d_i_new = (
                        x_d_i_new
                        + (
                            eyes_delta.reshape(-1, x_s.shape[1], 3)
                            if eyes_delta is not None
                            else 0
                        )
                        + (
                            lip_delta.reshape(-1, x_s.shape[1], 3)
                            if lip_delta is not None
                            else 0
                        )
                    )

                if inference_cfg.flag_stitching:
                    x_d_i_new = self.live_portrait_wrapper.stitching(x_s, x_d_i_new)

            if inference_cfg.flag_stitching:
                x_d_i_new = self.live_portrait_wrapper.stitching(x_s, x_d_i_new)

            out = self.live_portrait_wrapper.warp_decode(f_s, x_s, x_d_i_new)
            
            out_list.append(out)
    
            pbar.update(1)

        out_dict = {
            "out_list": out_list,
            "crop_info": crop_info,
            "mismatch_method": mismatch_method,
        }

        return out_dict

    def silence_lips(
        self,  crop_info,  delta_multiplier, relative_motion_mode="expression_only", driving_smooth_observation_variance=0.0003, mismatch_method="constant", 
    ):
        inference_cfg = self.live_portrait_wrapper.cfg
        device = inference_cfg.device_id

        out_list = []
        R_d_0, x_d_0_info = None, None

        source_images_num = len(crop_info["crop_info_list"])

        total_frames = source_images_num

        disable_progress_bar = True if relative_motion_mode == "single_frame" else False

        source_info = crop_info["source_info"]
        source_rot_list = crop_info["source_rot_list"]
        f_s_list = crop_info["f_s_list"]
        x_s_list = crop_info["x_s_list"]

        driving_info = []
        driving_exp_list = []
        driving_rot_list = []
        
        for i in tqdm(range(total_frames), desc='Processing driving images...', total=total_frames, disable=disable_progress_bar):
            #get driving keypoints info
            safe_index = min(i, source_images_num - 1)
            if crop_info["crop_info_list"][safe_index] is None:
                driving_info.append(None)
                driving_rot_list.append(None)
                driving_exp_list.append(None)
                if i == 0:
                    raise ValueError("No face detected in FIRST source image")
                continue
            x_s_info = source_info[safe_index]
            x_d_info = x_s_info
            
            if i == 0:
                first = x_d_info

            driving_info.append(x_d_info)

            driving_exp = x_d_info['exp']
            driving_exp_list.append(driving_exp.cpu())

            R_d = get_rotation_matrix(
                x_d_info["pitch"], x_d_info["yaw"], x_d_info["roll"]
            )
            driving_rot_list.append(R_d)

        if relative_motion_mode == "source_video_smoothed" or relative_motion_mode == "expression_only":
            x_d_r_lst = []
            first_driving_rot = driving_rot_list[0].cpu().numpy().astype(np.float32).transpose(0, 2, 1)
            for i in tqdm(range(source_images_num), desc='Smoothing...', total=source_images_num):
                if driving_rot_list[i] is None:
                    x_d_r_lst.append(None)
                    continue
                driving_rot = driving_rot_list[i].cpu().numpy().astype(np.float32)
                source_rot = source_rot_list[i].cpu().numpy().astype(np.float32)
                dot = np.dot(driving_rot, first_driving_rot) @ source_rot
                x_d_r_lst.append(dot)
  
            driving_exp_list_smooth = smooth(driving_exp_list, source_info[0]["exp"].shape, device, observation_variance=driving_smooth_observation_variance)
            driving_rot_list_smooth = smooth(x_d_r_lst, source_rot_list[0].shape, device, observation_variance=driving_smooth_observation_variance)

        pbar = comfy.utils.ProgressBar(total_frames)

        for i in tqdm(range(total_frames), desc='Animating...', total=total_frames, disable=disable_progress_bar):

            safe_index = min(i, len(crop_info["crop_info_list"]) - 1)

            # skip and return empty frames if no crop due to no face detected
            if crop_info["crop_info_list"][safe_index] is None:
                out_list.append({})
                pbar.update(1)
                continue

            source_lmk = crop_info["crop_info_list"][safe_index]["lmk_crop"]
            
            x_d_info = driving_info[i]
            R_d = driving_rot_list[i]

            x_s_info = source_info[safe_index]
            R_s = source_rot_list[safe_index]
            f_s = f_s_list[safe_index]
            x_s = x_s_list[safe_index]

            x_c_s = x_s_info["kp"]


            if relative_motion_mode == "source_video_smoothed":
                R_new = driving_rot_list_smooth[i]
                delta_new = driving_exp_list_smooth[i]
                scale_new = x_s_info["scale"]
                t_new = x_d_info["t"]
            elif relative_motion_mode == "expression_only":
                R_new = R_s
                # Initialize delta_new with the full expression data
                delta_new = x_s_info['exp'].clone()

                # Create a tensor with zeros and the lip array
                zeros_and_lip = torch.zeros_like(x_s_info['exp']) + torch.from_numpy(load_lip_array()).to(dtype=torch.float32, device=device)

                # Define the indices to be replaced
                # indices_to_replace = [1, 2, 6, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
                indices_to_replace = [1, 2, 6, 12, 14, 17, 19, 20]  ##indices_to_replace = [1, 2, 6, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]

                # Replace specific indices with data from zeros_and_lip
                for idx in indices_to_replace:
                    delta_new[:, idx, :] = zeros_and_lip[:, idx, :]
                # Additional specific replacements
                delta_new[:, 3:5, 1] = zeros_and_lip[:, 3:5, 1]
                delta_new[:, 5, 2] = zeros_and_lip[:, 5, 2]
                delta_new[:, 8, 2] = zeros_and_lip[:, 8, 2]
                delta_new[:, 9, 1:] = zeros_and_lip[:, 9, 1:]
                scale_new = x_s_info["scale"]
                t_new = x_d_info["t"]
            else:
                R_new = R_d
                delta_new = x_s_info['exp']
                scale_new = x_s_info["scale"]
                t_new = x_d_info["t"]

            t_new[..., 2].fill_(0)  # zero tz

            delta_new = delta_new * delta_multiplier
            
            x_d_i_new = scale_new * (x_c_s @ R_new + delta_new) + t_new

            if inference_cfg.flag_stitching:
                x_d_i_new = self.live_portrait_wrapper.stitching(x_s, x_d_i_new)

            out = self.live_portrait_wrapper.warp_decode(f_s, x_s, x_d_i_new)
            
            out_list.append(out)
    
            pbar.update(1)

        out_dict = {
            "out_list": out_list,
            "crop_info": crop_info,
            "mismatch_method": mismatch_method,
        }

        return out_dict
