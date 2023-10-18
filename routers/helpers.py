import asyncio
import io
from typing import List
from configuration.development_config import Settings
from DINet.utils.data_processing import compute_crop_radius
import shutil
import numpy as np
from numpy import ndarray
import os
import cv2
import torch
import random
from loguru import logger
from preprocess import get_DSModel, get_fa, model
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor
from typing import Dict
from asyncio import Task
import tempfile

_pool_executor: ProcessPoolExecutor = None


def get_pool_executor():
    global _pool_executor
    if _pool_executor is None:
        _pool_executor = ProcessPoolExecutor(max_workers=Settings().max_workers)
    return _pool_executor


def extract_frames_from_video(video_bytes: bytes):
    # todo 参考https://chat.openai.com/share/d4f58cc6-2ed8-4bf4-93f7-3de77ff841e1
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
    temp_file.write(video_bytes)
    temp_file.close()

    videoCapture = cv2.VideoCapture(temp_file.name)
    fps = videoCapture.get(cv2.CAP_PROP_FPS)
    if int(fps) != 25:
        # todo 转25fps
        logger.warning('The input video is not 25 fps, it would be better to trans it to 25 fps!')
    frames = videoCapture.get(cv2.CAP_PROP_FRAME_COUNT)
    frame_ndarrays: List[np.ndarray] = []
    for i in range(int(frames)):
        ret, frame = videoCapture.read()
        frame_ndarrays.append(frame)
    videoCapture.release()
    os.unlink(temp_file.name)
    frame_ndarrays: np.ndarray = np.stack(frame_ndarrays)
    return frame_ndarrays


def _get_frames_landmarks_pad(frames_ndarray: ndarray, video_landmark_data: ndarray, res_frame_length: int):
    """
    align frame with driving audio
    首位加点pad
    """
    video_frames_data_cycle = np.concatenate([frames_ndarray, np.flip(frames_ndarray, 0)], 0)
    video_landmark_data_cycle = np.concatenate([video_landmark_data, np.flip(video_landmark_data, 0)], 0)
    video_frames_data_cycle_length = len(video_frames_data_cycle)
    if video_frames_data_cycle_length >= res_frame_length:
        res_video_frames_data = video_frames_data_cycle[:res_frame_length, :, :, :]
        res_video_landmark_data = video_landmark_data_cycle[:res_frame_length, :, :]
    else:
        divisor = res_frame_length // video_frames_data_cycle_length
        remainder = res_frame_length % video_frames_data_cycle_length
        res_video_frames_data = np.concatenate(
            [video_frames_data_cycle] * divisor + video_frames_data_cycle[:remainder], 0)
        res_video_landmark_data = np.concatenate(
            [video_landmark_data_cycle] * divisor + [video_landmark_data_cycle[:remainder, :, :]], 0)
    res_video_frames_data_pad: ndarray = np.pad(res_video_frames_data, ((2, 2), (0, 0), (0, 0), (0, 0)), mode='edge')
    res_video_landmark_data_pad = np.pad(res_video_landmark_data, ((2, 2), (0, 0), (0, 0)), mode='edge')
    return res_video_frames_data_pad, res_video_landmark_data_pad


def _pick5frames(res_video_frames_data_pad: ndarray,
                 res_video_landmark_data_pad: ndarray,
                 resize_w: int,
                 resize_h: int):
    ref_index_list = random.sample(range(5, res_video_frames_data_pad.shape[0] - 2), 5)
    ref_img_list = []
    video_size = res_video_frames_data_pad.shape[1:3][::-1]
    for ref_index in ref_index_list:
        crop_flag, crop_radius = compute_crop_radius(video_size,
                                                     res_video_landmark_data_pad[ref_index - 5:ref_index, :, :])
        if not crop_flag:
            raise ValueError('our method can not handle videos with large change of facial size!!')
        crop_radius_1_4 = crop_radius // 4
        ref_img = res_video_frames_data_pad[ref_index - 3, :, :, ::-1]
        ref_landmark = res_video_landmark_data_pad[ref_index - 3, :, :]
        ref_img_crop = ref_img[
                       ref_landmark[29, 1] - crop_radius:
                       ref_landmark[29, 1] + crop_radius * 2 + crop_radius_1_4,
                       ref_landmark[33, 0] - crop_radius - crop_radius_1_4:
                       ref_landmark[33, 0] + crop_radius + crop_radius_1_4,
                       :]  # 裁剪鼻嘴部
        ref_img_crop = cv2.resize(ref_img_crop, (resize_w, resize_h))
        ref_img_crop = ref_img_crop / 255.0  # 颜色比例
        ref_img_list.append(ref_img_crop)
    ref_video_frame = np.concatenate(ref_img_list, 2)
    return ref_video_frame, video_size


def inf2video_file(ref_video_frame,
                   vid,
                   video_name,
                   video_size,
                   pad_length,
                   res_video_landmark_data_pad,
                   res_video_frames_data_pad,
                   resize_w,
                   resize_h,
                   mouth_region_size,
                   ds_feature_padding,
                   model):
    ref_img_tensor = torch.from_numpy(ref_video_frame).permute(2, 0, 1).unsqueeze(0).float().cuda()
    res_video_dir = f"./temp/{vid}"
    if os.path.exists(res_video_dir):
        try:
            shutil.rmtree(res_video_dir)
        except:
            ...
    os.mkdir(res_video_dir)

    res_video_path = os.path.join(res_video_dir, video_name + '_facial_dubbing.mp4')
    if os.path.exists(res_video_path):
        os.remove(res_video_path)
    videowriter = cv2.VideoWriter(res_video_path,
                                  cv2.VideoWriter_fourcc(*'XVID'),
                                  25,
                                  video_size)
    for clip_end_index in tqdm(range(5, pad_length, 1)):
        crop_flag, crop_radius = compute_crop_radius(
            video_size,
            res_video_landmark_data_pad[clip_end_index - 5:clip_end_index, :, :],
            random_scale=1.05)  # 5个图片一包，窗口移动处理
        if not crop_flag:
            raise ValueError('our method can not handle videos with large change of facial size!!')
        crop_radius_1_4 = crop_radius // 4
        frame_data = res_video_frames_data_pad[clip_end_index - 3, :, :, ::-1]  # 包里面5个图片的中间那个
        frame_landmark = res_video_landmark_data_pad[clip_end_index - 3, :, :]
        crop_frame_data = frame_data[
                          frame_landmark[29, 1] - crop_radius:frame_landmark[29, 1] + crop_radius * 2 + crop_radius_1_4,
                          frame_landmark[33, 0] - crop_radius - crop_radius_1_4:frame_landmark[
                                                                                    33, 0] + crop_radius + crop_radius_1_4,
                          :]  # 裁剪面部
        crop_frame_h, crop_frame_w = crop_frame_data.shape[0], crop_frame_data.shape[1]
        crop_frame_data = cv2.resize(crop_frame_data, (resize_w, resize_h))  # [32:224, 32:224, :]
        crop_frame_data = crop_frame_data / 255.0
        # todo 平均亮度校正
        crop_frame_data[mouth_region_size // 2:mouth_region_size // 2 + mouth_region_size,
        mouth_region_size // 8:mouth_region_size // 8 + mouth_region_size, :] = 0
        crop_frame_tensor = torch.from_numpy(crop_frame_data).float().cuda().permute(2, 0, 1).unsqueeze(0)
        deepspeech_tensor = torch.from_numpy(
            ds_feature_padding[clip_end_index - 5:clip_end_index, :]).permute(1, 0).unsqueeze(0).float().cuda()
        with torch.no_grad():
            pre_frame = model(crop_frame_tensor, ref_img_tensor, deepspeech_tensor)
            pre_frame = pre_frame.squeeze(0).permute(1, 2, 0).detach().cpu().numpy() * 255  # 面部
        pre_frame_resize = cv2.resize(pre_frame, (crop_frame_w, crop_frame_h))  # 恢复原面部高宽
        frame_data[
        frame_landmark[29, 1] - crop_radius:
        frame_landmark[29, 1] + crop_radius * 2,
        frame_landmark[33, 0] - crop_radius - crop_radius_1_4:
        frame_landmark[33, 0] + crop_radius + crop_radius_1_4,
        :] = pre_frame_resize[:crop_radius * 3, :, :]  # 将推理的面部写回原帧
        videowriter.write(frame_data[:, :, ::-1])
    videowriter.release()
    # todo 添加声音
    # video_add_audio_path = res_video_path.replace('.mp4', '_add_audio.mp4')
    # if os.path.exists(video_add_audio_path):
    #     os.remove(video_add_audio_path)
    # cmd = 'ffmpeg -i {} -i {} -c:v copy -c:a aac -strict experimental -map 0:v:0 -map 1:a:0 {}'.format(
    #     res_video_path,
    #     opt.driving_audio_path,
    #     video_add_audio_path)
    # subprocess.call(cmd, shell=True)


async def inf_video(vid: str, video_name: str, video_bytes: bytes, audio_bytes: bytes):
    # 视频处理 得到视频的帧ndarray表示
    # 音频处理为推理所用特征值 todo 子进程计算，子进程to.cuda
    frames_ndarray, ds_feature = await asyncio.gather(
        asyncio.get_running_loop().run_in_executor(get_pool_executor(),
                                                   extract_frames_from_video,
                                                   video_bytes),
        asyncio.get_running_loop().run_in_executor(None,
                                                   DSModel.compute_audio_feature,
                                                   io.BytesIO(audio_bytes)))
    res_frame_length = ds_feature.shape[0]
    ds_feature_padding = np.pad(ds_feature, ((2, 2), (0, 0)), mode='edge')
    # 人脸68关键点
    fa = get_fa()
    batch_landmarks = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda frames: fa.get_landmarks_from_batch(torch.Tensor(frames.transpose(0, 3, 1, 2))),
        frames_ndarray)
    batch_landmarks = [landmarks[:68, :] for landmarks in batch_landmarks]
    video_landmark_data: np.ndarray = np.stack(batch_landmarks).astype(int)
    ############################################## align frame with driving audio ##############################################从视频无限回环中截取以对齐音频
    res_video_frames_data_pad, res_video_landmark_data_pad = await asyncio.get_running_loop().run_in_executor(
        get_pool_executor(), _get_frames_landmarks_pad, frames_ndarray, video_landmark_data, res_frame_length)
    assert ds_feature_padding.shape[0] == res_video_frames_data_pad.shape[0] == res_video_landmark_data_pad.shape[0]
    pad_length = ds_feature_padding.shape[0]
    ############################################## randomly select 5 reference images ##############################################
    mouth_region_size = Settings().mouth_region_size
    resize_w = int(mouth_region_size + mouth_region_size // 4)
    resize_h = int((mouth_region_size // 2) * 3 + mouth_region_size // 8)
    ref_video_frame, video_size = await asyncio.get_running_loop().run_in_executor(
        get_pool_executor(), _pick5frames, res_video_frames_data_pad, res_video_landmark_data_pad, resize_w, resize_h)
    ############################################## inference frame by frame ##############################################
    await asyncio.get_running_loop().run_in_executor(None,
                                                     inf2video_file,
                                                     ref_video_frame,
                                                     vid,
                                                     video_name,
                                                     video_size,
                                                     pad_length,
                                                     res_video_landmark_data_pad,
                                                     res_video_frames_data_pad,
                                                     resize_w,
                                                     resize_h,
                                                     mouth_region_size,
                                                     ds_feature_padding,
                                                     model)


async def delay_rm_video(delay_seconds: float, inf_video_tasks: Dict[str, Task], vid: str):
    """延迟删除视频文件"""
    await asyncio.sleep(delay_seconds)
    if vid in inf_video_tasks.keys(): inf_video_tasks.pop(vid)
    res_video_dir = f"./temp/{vid}"
    if os.path.exists(res_video_dir):
        try:
            shutil.rmtree(res_video_dir)
        except:
            ...
    logger.info(f"Video id:{vid} cleared.")
