import copy

import glob
import os
import cv2
import joblib
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from phalp.utils.utils import progress_bar
from phalp.utils.utils_tracks import create_fast_tracklets, get_tracks
from phalp.utils.utils import pose_camera_vector_to_smpl
from phalp.utils.lart_utils import to_ava_labels

CHECKPOINT_INTERVAL = 10 # in tracks

class Postprocessor(nn.Module):
    
    def __init__(self, cfg, phalp_tracker):
        super(Postprocessor, self).__init__()
        
        self.cfg = cfg
        self.device = 'cuda'
        self.phalp_tracker = phalp_tracker

    def post_process(self, final_visuals_dic, save_fast_tracks=False, video_pkl_name="", checkpoint_end=0):

        print("In post_process")
        if(self.cfg.post_process.apply_smoothing):
            print("Copying final visuals dictionary")
            final_visuals_dic_ = copy.deepcopy(final_visuals_dic)
            print("Loading tracks")
            #track_dict = get_tracks(final_visuals_dic_)
            cache_dir = self.cfg.video.output_dir + "/results_tracks/" + video_pkl_name + "/"
            get_tracks(final_visuals_dic_, cache_dir=cache_dir)
            
            track_dict = {}
            for track_fn in glob.glob(cache_dir + "*.pkl"):
                tid = int(track_fn.split("/")[-1].replace(".pkl", ""))
                print("Loading cached track frames for", tid)
                track_dict[tid] = joblib.load(track_fn)

            print("Total # of tracks:", len(list(track_dict.keys())))
            
            for t, tid_ in enumerate(sorted(track_dict.keys())):
                if t < checkpoint_end:
                    print("Skipping checkpointed track", tid_)
                    continue

                print("Working on track", tid_)
                fast_track_ = create_fast_tracklets(track_dict[tid_])
            
                with torch.no_grad():
                    smoothed_fast_track_ = self.phalp_tracker.pose_predictor.smooth_tracks(fast_track_, moving_window=True, step=32, window=32)

                if(save_fast_tracks):
                    frame_length = len(smoothed_fast_track_['frame_name'])
                    dict_ava_feat = {}
                    dict_ava_psudo_labels = {}
                    for idx, appe_idx in enumerate(smoothed_fast_track_['apperance_index']):
                        dict_ava_feat[appe_idx[0,0]] = smoothed_fast_track_['apperance_emb'][idx]
                        dict_ava_psudo_labels[appe_idx[0,0]] = smoothed_fast_track_['action_emb'][idx]
                    smoothed_fast_track_['action_label_gt'] = np.zeros((frame_length, 1, 80)).astype(int)
                    smoothed_fast_track_['action_label_psudo'] = dict_ava_psudo_labels
                    smoothed_fast_track_['apperance_dict'] = dict_ava_feat
                    smoothed_fast_track_['pose_shape'] = smoothed_fast_track_['pose_shape'].cpu().numpy()

                    # save the fast tracks in a pkl file
                    save_pkl_path = os.path.join(self.cfg.video.output_dir, "results_temporal_fast/", video_pkl_name + "_" + str(tid_) +  "_" + str(frame_length) + ".pkl")
                    joblib.dump(smoothed_fast_track_, save_pkl_path)

                for i_ in range(smoothed_fast_track_['pose_shape'].shape[0]):
                    f_key = smoothed_fast_track_['frame_name'][i_]
                    tids_ = np.array(final_visuals_dic_[f_key]['tid'])
                    idx_  = np.where(tids_==tid_)[0]
                    
                    if(len(idx_)>0):

                        pose_shape_ = smoothed_fast_track_['pose_shape'][i_]
                        smpl_camera = pose_camera_vector_to_smpl(pose_shape_[0])
                        smpl_ = smpl_camera[0]
                        camera = smpl_camera[1]
                        camera_ = smoothed_fast_track_['cam_smoothed'][i_][0].cpu().numpy()

                        dict_ = {}
                        for k, v in smpl_.items():
                            dict_[k] = v

                        if(final_visuals_dic[f_key]['tracked_time'][idx_[0]]>0):
                            final_visuals_dic[f_key]['camera'][idx_[0]] = np.array([camera_[0], camera_[1], 200*camera_[2]])
                            final_visuals_dic[f_key]['smpl'][idx_[0]] = copy.deepcopy(dict_)
                            final_visuals_dic[f_key]['tracked_time'][idx_[0]] = -1
                        
                        # attach ava labels
                        ava_ = smoothed_fast_track_['ava_action'][i_]
                        ava_ = ava_.cpu()
                        ava_labels, _ = to_ava_labels(ava_, self.cfg)
                        final_visuals_dic[f_key].setdefault('label', {})[tid_] = ava_labels
                        final_visuals_dic[f_key].setdefault('ava_action', {})[tid_] = ava_

                if((t > 0) and (t % CHECKPOINT_INTERVAL == 0)):
                    chkpt_path = os.path.join(self.cfg.video.output_dir, "results_temporal/", video_pkl_name + ".lart.pkl." + str(t))
                    joblib.dump(final_visuals_dic, chkpt_path)

        return final_visuals_dic

    def run_lart(self, phalp_pkl_path):
        
        # lart_output = {}
        print("PHALP running LART on pkl file", phalp_pkl_path)
        video_pkl_fn = phalp_pkl_path.split("/")[-1]
        if(video_pkl_fn.split(".")[-1].isnumeric()):
            video_pkl_name = ".".join(phalp_pkl_path.split("/")[-1].split(".")[:-1]).replace(".pkl", "").replace(".lart", "")
            checkpoint_end = int(video_pkl_fn.split(".")[-1])
            print("Restarting from checkpoint at track count", checkpoint_end)
        else:
            video_pkl_name = phalp_pkl_path.split("/")[-1].replace(".pkl", "")
            checkpoint_end = 0

        print("Loading PHALP .pkl file")
        # XXX Might be better to make this RAM-bound, not VRAM-bound
        #torch.serialization.register_package(0, lambda x: x.device.type, lambda x, _: x.cpu())
        final_visuals_dic = joblib.load(phalp_pkl_path)

        # PMB For caching LART track data rather than keeping it in memory
        os.makedirs(self.cfg.video.output_dir + "/results_tracks/" + video_pkl_name, exist_ok=True)
        os.makedirs(self.cfg.video.output_dir + "/results_temporal/", exist_ok=True)
        os.makedirs(self.cfg.video.output_dir + "/results_temporal_fast/", exist_ok=True)
        os.makedirs(self.cfg.video.output_dir + "/results_temporal_videos/", exist_ok=True)
        save_pkl_path = os.path.join(self.cfg.video.output_dir, "results_temporal/", video_pkl_name + ".lart.pkl")
        save_video_path = os.path.join(self.cfg.video.output_dir, "results_temporal_videos/", video_pkl_name + "_.mp4")

        if(os.path.exists(save_pkl_path) and not(self.cfg.overwrite)):
            return 0
        
        # apply smoothing/action recognition etc.
        final_visuals_dic  = self.post_process(final_visuals_dic, save_fast_tracks=self.cfg.post_process.save_fast_tracks, video_pkl_name=video_pkl_name, checkpoint_end=checkpoint_end)
        
        # render the video
        if(self.cfg.render.enable):
            self.offline_render(final_visuals_dic, save_pkl_path, save_video_path)
        
        joblib.dump(final_visuals_dic, save_pkl_path)

    def run_renderer(self, phalp_pkl_path):
        
        video_pkl_name = phalp_pkl_path.split("/")[-1].split(".")[0]
        final_visuals_dic = joblib.load(phalp_pkl_path)

        os.makedirs(self.cfg.video.output_dir + "/videos/", exist_ok=True)
        os.makedirs(self.cfg.video.output_dir + "/videos_tmp/", exist_ok=True)
        save_pkl_path = os.path.join(self.cfg.video.output_dir, "videos_tmp/", video_pkl_name + ".pkl")
        save_video_path = os.path.join(self.cfg.video.output_dir, "videos/", video_pkl_name + ".mp4")

        if(os.path.exists(save_pkl_path) and not(self.cfg.overwrite)):
            return 0
        
        # render the video
        self.offline_render(final_visuals_dic, save_pkl_path, save_video_path)


    def offline_render(self, final_visuals_dic, save_pkl_path, save_video_path):
        
        video_pkl_name = save_pkl_path.split("/")[-1].split(".")[0]
        list_of_frames = list(final_visuals_dic.keys())
        
        for t_, frame_path in progress_bar(enumerate(list_of_frames), description="Rendering : " + video_pkl_name, total=len(list_of_frames), disable=False):
            
            image = self.phalp_tracker.io_manager.read_frame(frame_path)

            ################### Front view #########################
            self.cfg.render.up_scale = int(self.cfg.render.output_resolution / self.cfg.render.res)
            self.phalp_tracker.visualizer.reset_render(self.cfg.render.res*self.cfg.render.up_scale)
            final_visuals_dic[frame_path]['frame'] = image
            panel_render, f_size = self.phalp_tracker.visualizer.render_video(final_visuals_dic[frame_path])      
            del final_visuals_dic[frame_path]['frame']

            # resize the image back to render resolution
            panel_rgb = cv2.resize(image, (f_size[0], f_size[1]), interpolation=cv2.INTER_AREA)

            # save the predicted actions labels
            if('label' in final_visuals_dic[frame_path]):
                labels_to_save = []
                for tid_ in final_visuals_dic[frame_path]['label']:
                    ava_labels = final_visuals_dic[frame_path]['label'][tid_]
                    labels_to_save.append(ava_labels)
                labels_to_save = np.array(labels_to_save)

            panel_1 = np.concatenate((panel_rgb, panel_render), axis=1)
            final_panel = panel_1

            self.phalp_tracker.io_manager.save_video(save_video_path, final_panel, (final_panel.shape[1], final_panel.shape[0]), t=t_)
            t_ += 1

        self.phalp_tracker.io_manager.close_video()


