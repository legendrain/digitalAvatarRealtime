import os
import glob
from typing import Dict
import face_alignment
import torch
from DINet.models.DINet import DINet
from objects import VideoFrames
from collections import OrderedDict
from DINet.utils.deep_speech import DeepSpeech

video_full_frames: Dict[str, VideoFrames] = {}
# 人脸检测
fa = None
# 推理模型
model = None
# deepspeech 模型
DSModel = None


def get_fa():
    """获取FaceAlignment实例"""
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    global fa
    if fa is None:
        fa = face_alignment.FaceAlignment(
            face_alignment.LandmarksType.TWO_HALF_D, device=device, face_detector='blazeface')
    return fa


def preload_videos():
    """
    预加载视频文件，存为内存数据供推理用
    """
    # 指定视频文件的扩展名
    video_extensions = ["*.mp4", "*.avi", "*.mkv", "*.flv", "*.mov", "*.wmv"]
    # 指定需要搜索的文件夹
    folder = os.path.join(os.getcwd(), 'faces')
    # 用于存储找到的所有视频文件的路径
    videos = []
    for video_extension in video_extensions:
        # os.path.join用于合并路径
        # glob.glob返回所有匹配的文件路径列表
        videos.extend(glob.glob(os.path.join(folder, video_extension)))

    # 迭代找到的视频文件路径，转成video_frames
    for video in videos:
        vff = VideoFrames(video)
        vff.gen_frames()
        video_full_frames[os.path.splitext(os.path.basename(video))[0]] = vff


def load_model():
    """加载模型到GPU"""
    # DINet预训练模型
    global model
    model = DINet(3, 15, 29).cuda()
    pretrained_clip_DINet_path = "./DINet/asserts/clip_training_DINet_256mouth.pth"
    if not os.path.exists(pretrained_clip_DINet_path):
        raise FileNotFoundError(
            'wrong path of pretrained model weight: {}。Reference "https://github.com/monk-after-90s/DINet" to download.'.format(
                pretrained_clip_DINet_path))
    state_dict = torch.load(pretrained_clip_DINet_path)['state_dict']['net_g']
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        name = k[7:]  # remove module.
        new_state_dict[name] = v
    model.load_state_dict(new_state_dict)
    model.eval()
    # deepspeech模型
    deepspeech_model_path = "./DINet/asserts/output_graph.pb"
    if not os.path.exists(deepspeech_model_path):
        raise FileNotFoundError(
            'pls download pretrained model of deepspeech.Reference "https://github.com/monk-after-90s/DINet" to download.')
    global DSModel
    DSModel = DeepSpeech(deepspeech_model_path)
