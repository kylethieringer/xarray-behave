"""Create self-documenting xarray dataset from behavioral recordings and annotations."""
import numpy as np
from samplestamps.samplestamps import SampStamp
import scipy.interpolate
import scipy.stats
import xarray as xr
import zarr
import logging
import os.path
from pathlib import Path
import pandas as pd
from glob import glob
from typing import List, Optional
from . import (loaders as ld,
               metrics as mt,
               event_utils,
               annot,
               io)


def assemble(datename, root='', dat_path='dat', res_path='res',
             filepath_timestamps: Optional[str] = None,
             filepath_video: Optional[str] = None,
             filepath_daq: Optional[str] = None,
             target_sampling_rate=1_000, resample_video_data: bool = True,
             include_song: bool = True, include_tracks: bool = True, include_poses: bool = True, include_balltracker: bool = True, include_movieparams: bool = True,
             fix_fly_indices: bool = True, pixel_size_mm: Optional[float] = None,
             lazy_load_song: bool = False) -> xr.Dataset:
    """[summary]

    Args:
        datename ([type]): Used to infer file paths.
                           Constructed paths follow this schema f"{root}/{dat_path|res_path}/{datename}/{datename}_suffix.extension".
                           Can be overridden for some paths with arguments below.
        root (str, optional): Used to infer file paths. Defaults to ''.
        dat_path (str, optional): Used to infer paths of video, daq, and timestamps files. Defaults to 'dat'.
        res_path (str, optional): Used to infer paths of annotation, tracking etc files. Defaults to 'res'.
        filepath_timestamps (str, optional): Path to the timestamps file. Defaults to None (infer path from `datename` and `dat_path`).
        filepath_video (str, optional): Path to the video file. Defaults to None (infer path from `datename` and `dat_path`).
        filepath_daq (str, optional): Path to the daq file. Defaults to None (infer path from `datename` and `dat_path`).
        target_sampling_rate (int, optional): Sampling rate in Hz for pose and annotation data. Defaults to 1000.
        keep_multi_channel (bool, optional): Add multi-channel data (otherwise will only add merged traces). Defaults to False.
        resample_video_data (bool, optional): Or keep video with original frame times. Defaults to True.
        include_song (bool, optional): [description]. Defaults to True.
        include_tracks (bool, optional): [description]. Defaults to True.
        include_poses (bool, optional): [description]. Defaults to True.
        include_balltracker (bool, optional): [description]. Defaults to True.
        include_balltracker (bool, optional): [description]. Defaults to True.
        fix_fly_indices (bool, optional): Will attempt to load swap info and fix fly id's accordingly, Defaults to True.
        pixel_size_mm (float, optional): Size of a pixel (in mm) in the video. Used to convert tracking data to mm.
        lazy_load_song (float): Memmap data via dask. If false, full array will be loaded into memory. Defaults to False
    Returns:
        xarray.Dataset
    """

    # load RECORDING and TIMING INFORMATION
    if filepath_daq is None:
        filepath_daq = Path(root, dat_path, datename, f'{datename}_daq.h5')
        filepath_daq_is_custom = False
    else:
        filepath_daq_is_custom = True

    if filepath_video is None:
        filepath_video = str(Path(root, dat_path, datename, f'{datename}.mp4'))
    # timestamps should be: filepath_video - ext + `_timestamps.h5`
    if filepath_timestamps is None:
        filepath_timestamps = Path(root, dat_path, datename, f'{datename}_timeStamps.h5')

    if os.path.exists(filepath_daq) and os.path.exists(filepath_timestamps):
        ss, last_sample_number, sampling_rate = ld.load_times(filepath_timestamps, filepath_daq)
    elif os.path.exists(filepath_video):
        from videoreader import VideoReader
        vr = VideoReader(filepath_video)
        frame_times = np.arange(0, vr.number_of_frames, 1) / vr.frame_rate
        frame_times[-1] = frame_times[-2]  # for auto-monotonize to not mess everything up
        sampling_rate = 2 * target_sampling_rate  #vr.frame_rate
        sample_times = np.arange(0, vr.number_of_frames, 1 / sampling_rate) / vr.frame_rate
        last_sample_number = len(sample_times)

        ss = SampStamp(sample_times, frame_times)

    filepath_timestamps_ball = Path(root, dat_path, datename, f'{datename}_ball_timeStamps.h5')
    filepath_timestamps_movie = Path(root, dat_path, datename, f'{datename}_movieframes.csv')
    if os.path.exists(filepath_daq) and os.path.exists(filepath_timestamps_ball):
        ss_ball, last_sample_number_ball, sampling_rate_ball = ld.load_times(filepath_timestamps_ball, filepath_daq)

    filepath_timestamps_movie = Path(root, res_path, datename, f'{datename}_movieframes.csv')
    if os.path.exists(filepath_daq) and os.path.exists(filepath_timestamps_movie):
        ss_movie, last_sample_number_movie, sampling_rate_movie = ld.load_movietimes(filepath_timestamps_movie, filepath_daq)

    if target_sampling_rate == 0:
        resample_video_data = False
    if not resample_video_data:
        logging.info(f'  setting targetsamplingrate to avg. fps.')
        target_sampling_rate = 1 / np.mean(np.diff(ss.frames2times.y))


    # LOAD TRACKS
    with_tracks = False
    with_fixed_tracks = False
    if include_tracks:
        logging.info('Loading tracks:')
        tracks_loader = io.get_loader(kind='tracks', basename=os.path.join(root, res_path, datename, datename))
        if tracks_loader:
            try:
                xr_tracks = tracks_loader.make(tracks_loader.path)
                xr_tracks = add_time(xr_tracks, ss, dim='frame_number')

                with_fixed_tracks = tracks_loader.path.endswith('_tracks_fixed.h5')
                logging.info(f'  {tracks_loader.path} loaded.')
                with_tracks = True
            except Exception as e:
                logging.info(f'  Loading {tracks_loader.path} failed.')
                logging.exception(e)
        else:
            logging.info('   Found no tracks.')
        logging.info('Done.')

    # LOAD POSES
    with_poses = False
    poses_from = None
    if include_poses:
        logging.info(f'Loading poses:')
        poses_loader = io.get_loader(kind='poses', basename=os.path.join(root, res_path, datename, datename))
        if poses_loader:
            try:
                xr_poses, xr_poses_allo = poses_loader.make(poses_loader.path)
                xr_poses = add_time(xr_poses, ss, dim='frame_number')
                xr_poses_allo = add_time(xr_poses_allo, ss, dim='frame_number')

                with_poses = True
                poses_from = poses_loader.NAME
                logging.info(f'   {poses_loader.path} loaded.')
            except Exception as e:
                logging.info(f'   Loading {poses_loader.path} failed.')
                logging.exception(e)
        else:
            logging.info(f'   Found no poses.')
        logging.info('Done.')

    # LOAD BALLTRACKER
    with_balltracker = False
    if include_balltracker:
        logging.info(f'Loading ball tracker:')
        balltracker_loader = io.get_loader(kind='balltracks', basename=os.path.join(root, res_path, datename, datename))
        if balltracker_loader:
            try:
                xr_balltracks = balltracker_loader.make(balltracker_loader.path)
                xr_balltracks = add_time(xr_balltracks, ss_ball, dim='frame_number_ball', suffix='_ball')

                logging.info(f'   {balltracker_loader.path} loaded.')
                with_balltracker = True
            except Exception as e:
                logging.info(f'   Loading {balltracker_loader.path} failed.')
                logging.exception(e)
        else:
            logging.info(f'   Found no balltracker data.')
        logging.info('Done.')

    # LOAD MOVIEPARAMS
    with_movieparams = False
    if include_movieparams:
        logging.info(f'Loading movie params:')
        movieparams_loader = io.get_loader(kind='movieparams', basename=os.path.join(root, dat_path, datename, datename))
        if movieparams_loader:
            try:
                xr_movieparams = movieparams_loader.make(movieparams_loader.path)
                xr_movieparams = add_time(xr_movieparams, ss_movie, dim='frame_number_movie',  suffix='_movie')

                logging.info(f'   {movieparams_loader.path} loaded.')
                with_movieparams = True
            except Exception as e:
                logging.info(f'   Loading {movieparams_loader.path} failed.')
                logging.exception(e)
        else:
            logging.info(f'   Found no movie params data.')
        logging.info('Done.')


    # Init empty audio and event data
    song_raw = None
    non_song_raw = None
    auto_event_seconds = {}
    auto_event_categories = {}
    manual_event_seconds = {}
    manual_event_categories = {}

    if include_song:
        logging.info(f'Loading automatic annotations:')
        annot_loader = io.get_loader(kind='annotations', basename=os.path.join(root, res_path, datename, datename), stop_after_match=False)
        if annot_loader:
            if isinstance(annot_loader, str):
                annot_loader = list(annot_loader)

            for al in annot_loader:
                try:
                    this_event_seconds, this_event_categories = al.load()
                    auto_event_seconds.update(this_event_seconds)
                    auto_event_categories.update(this_event_categories)
                    logging.info(f'   {al.path} loaded.')
                except Exception as e:
                    logging.info(f'   Loading {al.path} failed.')
                    logging.exception(e)
        else:
            logging.info(f'   Found no automatic annotations.')
        logging.info('Done.')

        # load MANUAL SONG ANNOTATIONS
        logging.info(f'Loading manual annotations:')
        manual_annot_loader = io.get_loader(kind='annotations_manual', basename=os.path.join(root, res_path, datename, datename))
        if manual_annot_loader:
            try:
                manual_event_seconds, manual_event_categories = manual_annot_loader.load(manual_annot_loader.path)
                logging.info(f'   {manual_annot_loader.path} loaded.')
            except Exception as e:
                logging.info(f'   Loading {manual_annot_loader.path} failed.')
                logging.exception(e)

        else:
            logging.info(f'   Found no manual automatic annotations.')
        logging.info('Done.')

        # load RAW song traces
        logging.info(f'Loading audio data:')
        if not filepath_daq_is_custom:
            basename = os.path.join(root, dat_path, datename, datename)
        else:
            basename = filepath_daq

        audio_loader = io.get_loader(kind='audio',
                                     basename=basename,
                                     basename_is_full_name=filepath_daq_is_custom)
        if audio_loader:
            try:
                song_raw, non_song_raw, samplerate = audio_loader.load(audio_loader.path, return_nonsong_channels=True, lazy=lazy_load_song)
                logging.info(f'   {audio_loader.path} loaded.')
            except Exception as e:
                logging.info(f'   Loading {audio_loader.path} failed..')
                logging.exception(e)
        logging.info('Done.')

    # merge manual and auto events -
    # manual will overwrite existing auto events with the same key
    event_seconds = auto_event_seconds.copy()
    event_seconds.update(manual_event_seconds)
    event_categories = auto_event_categories.copy()
    event_categories.update(manual_event_categories)

    event_seconds = ld.fix_keys(event_seconds)
    event_categories = ld.fix_keys(event_categories)

    # TODO:
    # - get timing from `ss_ball` if `ss` does not exist?
    # - cope with recordings w/o daq or video!

    # PREPARE sample/time/framenumber grids
    if not with_tracks:
        first_tracked_frame = int(ss.frame(0))
        last_tracked_frame = int(ss.frame(last_sample_number))
        logging.info(f'No tracks - setting first/last tracked frame numbers to those of the first/last sample in the recording ({first_tracked_frame}, {last_tracked_frame}).')
    else:
        first_tracked_frame, last_tracked_frame = int(xr_tracks.frame_number[0]), int(xr_tracks.frame_number[-1])
        logging.info(f'Tracked frame {first_tracked_frame} to {last_tracked_frame}.')

    # construct desired sample grid for data
    step = sampling_rate / target_sampling_rate
    if resample_video_data:
        # in case the DAQ stopped before the video
        last_sample_with_frame = np.min((last_sample_number, ss.sample(frame=last_tracked_frame - 1))).astype(np.intp)
        target_samples = np.arange(0, last_sample_with_frame, step, dtype=np.uintp)
    else:
        frame_numbers = np.arange(first_tracked_frame, last_tracked_frame)
        frame_samples = ss.sample(frame_numbers)
        frame_samples = interp_duplicates(frame_samples)
        target_samples = frame_samples

    ref_time = ss.sample_time(0)  # first sample corresponds to 0 seconds
    time = ss.sample_time(target_samples) - ref_time

    # PREPARE DataArrays
    dataset_data = dict()

    logging.info('Making all datasets:')
    if song_raw is not None:
        if 0 not in song_raw.shape:  # xr fails saving zarr files with 0-size along any dim
            song_raw = xr.DataArray(data=song_raw[:, :],  # cut recording to match new grid
                                    dims=['sampletime', 'channels'],
                                    coords={'sampletime': ss.sample_time(np.arange(song_raw.shape[0])) - ref_time, },
                                    attrs={'description': 'Raw song recording (multi channel).',
                                        'sampling_rate_Hz': sampling_rate,
                                        'time_units': 'seconds',
                                        'amplitude_units': 'volts'})
            dataset_data['song_raw'] = song_raw

    if non_song_raw is not None:
        if 0 not in non_song_raw.shape:  # xr fails saving zarr files with 0-size along any dim
            non_song_raw = xr.DataArray(data=non_song_raw[:, :],  # cut recording to match new grid
                                        dims=['sampletime', 'no_song_channels'],
                                        coords={'sampletime': ss.sample_time(np.arange(non_song_raw.shape[0])) - ref_time, },
                                        attrs={'description': 'Non song (stimulus) data.',
                                            'sampling_rate_Hz': sampling_rate,
                                            'time_units': 'seconds',
                                            'amplitude_units': 'volts'})
            dataset_data['non_song_raw'] = non_song_raw

    logging.info('   segmentations')
    song_events = np.zeros((len(time), len(event_seconds)), dtype=np.int16)
    song_events = xr.DataArray(data=song_events,  # start with empty song_events matrix
                                dims=['time', 'event_types'],
                                coords={'time': time,
                                        'event_types': list(event_seconds.keys()),
                                        'event_categories': (('event_types'), list(event_categories.values())),},
                                attrs={'description': 'Event times as boolean arrays.',
                                        'sampling_rate_Hz': sampling_rate / step,
                                        'time_units': 'seconds',
                                        'event_times': event_seconds})
    # now populate from event_times attribute
    song_events_ds = event_utils.eventtimes_to_traces(song_events.to_dataset(name='song_events'),
                                                        song_events.attrs['event_times'])
    dataset_data['song_events'] = song_events_ds.song_events
    try:
        ds_eventtimes = annot.Events(event_seconds).to_dataset()
        dataset_data['event_times'] = ds_eventtimes.event_times
        dataset_data['event_names'] = ds_eventtimes.event_names
        dataset_data['event_names'].attrs['possible_event_names'] = ds_eventtimes.attrs['possible_event_names']
    except Exception as e:
        logging.error('Failed to generate event_times data arrays:')
        logging.exception(e)

    # BODY POSITION
    fps = None
    if pixel_size_mm is None:
        pixel_size_mm = np.nan

    if with_tracks:
        logging.info('   tracking')
        xr_tracks = align_time(xr_tracks, ss, target_samples, ref_time=ref_time, target_time=time, extrapolate=True)
        xr_tracks.attrs.update({'description': 'coords are "allocentric" - rel. to the full frame',
                                        'sampling_rate_Hz': sampling_rate / step,
                                        'time_units': 'seconds',
                                        'video_fps': fps,
                                        'spatial_units': 'pixels',
                                        'pixel_size_mm': pixel_size_mm,
                                        'tracks_fixed': with_fixed_tracks})
        dataset_data['body_positions'] = xr_tracks

    # POSES
    if with_poses:
        logging.info('   poses')
        xr_poses = align_time(xr_poses, ss, target_samples, ref_time=ref_time, target_time=time)
        xr_poses.attrs.update({'description': 'coords are "egocentric" - rel. to box',
                                    'sampling_rate_Hz': sampling_rate / step,
                                    'time_units': 'seconds',
                                    'video_fps': fps,
                                    'spatial_units': 'pixels',
                                    'pixel_size_mm': pixel_size_mm,
                                    'poses_from': poses_from})
        dataset_data['pose_positions'] = xr_poses

        # poses in ALLOcentric (frame-relative) coordinates
        xr_poses_allo = align_time(xr_poses_allo, ss, target_samples, ref_time=ref_time, target_time=time)
        xr_poses_allo.attrs.update({'description': 'coords are "allocentric" - rel. to frame',
                                         'sampling_rate_Hz': sampling_rate / step,
                                         'time_units': 'seconds',
                                         'video_fps': fps,
                                         'spatial_units': 'pixels',
                                         'pixel_size_mm': pixel_size_mm,
                                         'poses_from': poses_from})

        dataset_data['pose_positions_allo'] = xr_poses_allo

    # BALLTRACKS
    if with_balltracker:
        logging.info('   balltracker')
        xr_balltracks = align_time(xr_balltracks, ss_ball, target_samples, target_time=time,
                                   dim='frame_number_ball', suffix='_ball', ref_time=ref_time)
        xr_balltracks.attrs.update({'description': '',
                                    'sampling_rate_Hz': sampling_rate / step,
                                    'time_units': 'seconds',
                                    'video_fps': fps,})
        dataset_data['balltracks'] = xr_balltracks

    # MOVIEPARAMS
    if with_movieparams:
        logging.info('   movieparams')
        xr_movieparams = align_time(xr_movieparams, ss_movie, target_samples, dim='frame_number_movie', suffix='_movie',
                                    ref_time=ref_time, target_time=time, extrapolate=True)
        xr_movieparams.attrs.update({'description': '',
                                     'sampling_rate_Hz': sampling_rate / step,
                                     'time_units': 'seconds',
                                     'video_fps': fps,})
        dataset_data['movieparams'] = xr_movieparams

    # MAKE THE DATASET
    logging.info('   assembling')

    dataset = xr.Dataset(dataset_data, attrs={})
    if 'time' not in dataset:
        dataset.coords['time'] = time
    if 'sampletime' not in dataset:
        dataset.coords['sampletime'] = time
    if 'nearest_frame' not in dataset:
    #     # dataset.coords['nearest_frame'] = (('time'), (dataset['time'] + ref_time).astype(np.intp))  # ???
    #     # dataset.coords['nearest_frame'] = (('time'), (np.arange(len(dataset['time']), dtype=np.intp)))
         dataset.coords['nearest_frame'] = (('time'), (ss.times2frames(dataset['time'] + ref_time).astype(np.intp)))

    # convert spatial units to mm using info in attrs
    dataset = convert_spatial_units(dataset, to_units='mm',
                                    names = ['body_positions', 'pose_positions', 'pose_positions_allo'])

    if target_sampling_rate is None:
        target_sampling_rate = fps
    # save command line args
    dataset.attrs = {'video_filename': str(Path(root, dat_path, datename, f'{datename}.mp4')),
                     'datename': datename, 'root': root, 'dat_path': dat_path, 'res_path': res_path,
                     'target_sampling_rate_Hz': target_sampling_rate, 'ref_time': ref_time}

    filepath_swap = Path(root, res_path, datename, f'{datename}_idswaps.txt')
    if fix_fly_indices and os.path.exists(filepath_swap):
        logging.info('   applying fly identity fixes')
        try:
            indices, flies1, flies2 = ld.load_swap_indices(filepath_swap)
            dataset = ld.swap_flies(dataset, indices, flies1=0, flies2=1)
            logging.info(f'  Fixed fly identities using info from {filepath_swap}.')
        except (FileNotFoundError, OSError) as e:
            logging.debug(f'  Could not load fly identities using info from {filepath_swap}.')
            logging.debug(e)
    logging.info('Done.')
    return dataset


def add_time(ds, ss, dim: str = 'frame_number', suffix: str = ''):
    ds = ds.assign_coords({'frametimes' + suffix: (dim, ss.frame_time(frame=ds[dim]))})
    ds = ds.assign_coords({'framesamples' + suffix: (dim, ss.sample(frame=ds[dim]))})
    return ds


def align_time(ds, ss, target_samples, target_time, ref_time, dim: str = 'frame_number',
               suffix: str = '', time=None, extrapolate: bool = False):

    target_frames_float = ss.times2frames(ss.sample_time(target_samples))
    interp_kwargs = {}
    if extrapolate:
        interp_kwargs['fill_value'] = 'extrapolate'
    ds = ds.interp({dim: target_frames_float}, assume_sorted=True, kwargs=interp_kwargs)
    ds = ds.drop_vars(['frame_number' + suffix, 'frame_times' + suffix, 'frame_samples' + suffix], errors='Ignore')

    # time_new = ds['frametimes' + suffix] - ref_time
    ds = ds.assign_coords({'time': ((dim), target_time)})
    ds = ds.swap_dims({dim: 'time'})
    ds = ds.assign_coords({'nearest_frame' + suffix: (('time'), np.round(target_frames_float).astype(np.intp))})

    fps = 1 / np.nanmean(np.diff(ds['frametimes' + suffix]))
    ds.attrs.update({'video_fps': fps})

    ds = ds.drop_vars(['frame_number' + suffix, 'frame_times' + suffix, 'frame_samples' + suffix,
                       'framenumber' + suffix, 'frametimes' + suffix, 'framesamples' + suffix], errors='Ignore')

    return ds


def assemble_metrics(dataset, make_abs: bool = True, make_rel: bool = True, smooth_positions: bool = True):
    """[summary]

    Args:
        dataset ([type]): [description]
        make_abs (bool, optional): [description]. Defaults to True.
        make_rel (bool, optional): [description]. Defaults to True.
        smooth_positions (bool, optional): [description]. Defaults to True.

    Returns:
        [type]: [description]
        xarray.Dataset: containing these features:
                angles [time,flies]
                vels [time,flies,forward/lateral]
                chamber_vels [time,flies,y/x]
                rotational_speed [time,flies]
                accelerations [time,flies,forward/lateral]
                chamber_acc [time,flies,y/x]
                rotational_acc [time,flies]
                wing_angle_left [time,flies]
                wing_angle_right [time,flies]
                wing_angle_sum [time,flies]
                relative angle [time,flies,flies]
                (relative orientation) [time,flies,flies]
                (relative velocities) [time,flies,flies,y/x]
    """

    time = dataset.time
    nearest_frame = dataset.nearest_frame

    sampling_rate = dataset.pose_positions.attrs['sampling_rate_Hz']
    frame_rate = dataset.pose_positions.attrs['video_fps']

    thoraces = dataset.pose_positions_allo.loc[:, :, 'thorax', :].values.astype(np.float32)
    heads = dataset.pose_positions_allo.loc[:, :, 'head', :].values.astype(np.float32)
    wing_left = dataset.pose_positions_allo.loc[:, :, 'left_wing', :].values.astype(np.float32)
    wing_right = dataset.pose_positions_allo.loc[:, :, 'right_wing', :].values.astype(np.float32)

    if smooth_positions:
        # Smoothing window should span 2 frames to get smooth acceleration traces.
        # Since std of Gaussian used for smoothing has std of winlen/8, winlen should span 16 frames.
        winlen = np.ceil(16 / frame_rate * sampling_rate)
        thoraces = mt.smooth(thoraces, winlen)
        heads = mt.smooth(heads, winlen)
        wing_left = mt.smooth(wing_left, winlen)
        wing_right = mt.smooth(wing_right, winlen)

    angles = mt.angle(thoraces, heads)
    chamber_vels = mt.velocity(thoraces, ref='chamber')

    ds_dict = dict()
    if make_abs:
        vels = mt.velocity(thoraces, heads)
        accelerations = mt.acceleration(thoraces, heads)
        chamber_acc = mt.acceleration(thoraces, ref='chamber')

        vels_x = chamber_vels[..., 1]
        vels_y = chamber_vels[..., 0]
        vels_forward = vels[..., 0]
        vels_lateral = vels[..., 1]
        vels_mag = np.linalg.norm(vels, axis=2)
        accs_x = chamber_acc[..., 1]
        accs_y = chamber_acc[..., 0]
        accs_forward = accelerations[..., 0]
        accs_lateral = accelerations[..., 1]
        accs_mag = np.linalg.norm(accelerations, axis=2)

        rotational_speed = mt.rot_speed(thoraces, heads)
        rotational_acc = mt.rot_acceleration(thoraces, heads)

        # wing_angle_left = mt.angle(heads, thoraces) - mt.angle(thoraces, wing_left)
        # wing_angle_right = -(mt.angle(heads, thoraces) - mt.angle(thoraces, wing_right))
        # wing_angle_sum = mt.internal_angle(wing_left,thoraces,wing_right)
        wing_angle_left = 180-mt.internal_angle(wing_left,thoraces,heads)
        wing_angle_right = 180-mt.internal_angle(wing_right,thoraces,heads)
        wing_angle_sum = wing_angle_left + wing_angle_right

        list_absolute = [
            angles, rotational_speed, rotational_acc,
            vels_mag, vels_x, vels_y, vels_forward, vels_lateral,
            accs_mag, accs_x, accs_y, accs_forward, accs_lateral,
            wing_angle_left, wing_angle_right, wing_angle_sum
        ]

        abs_feature_names = [
            'angles', 'rotational_speed', 'rotational_acceleration',
            'velocity_magnitude', 'velocity_x', 'velocity_y', 'velocity_forward', 'velocity_lateral',
            'acceleration_mag', 'acceleration_x', 'acceleration_y', 'acceleration_forward', 'acceleration_lateral',
            'wing_angle_left', 'wing_angle_right', 'wing_angle_sum'
        ]

        absolute = np.stack(list_absolute, axis=2)

        ds_dict['abs_features'] = xr.DataArray(data=absolute,
                                               dims=['time', 'flies', 'absolute_features'],
                                               coords={'time': time,
                                                       'absolute_features': abs_feature_names,
                                                       'nearest_frame': (('time'), nearest_frame)},
                                               attrs={'description': 'coords are "egocentric" - rel. to box',
                                                      'sampling_rate_Hz': sampling_rate,
                                                      'time_units': 'seconds',
                                                      'spatial_units': 'pixels'})
    if make_rel:
        # RELATIVE FEATURES #
        dis = mt.distance(thoraces)
        rel_angles = mt.relative_angle(thoraces, heads)
        rel_orientation = angles[:, np.newaxis, :] - angles[:, :, np.newaxis]
        rel_velocities_y = chamber_vels[..., 0][:, np.newaxis, :] - chamber_vels[..., 0][:, :, np.newaxis]
        rel_velocities_x = chamber_vels[..., 1][:, np.newaxis, :] - chamber_vels[..., 1][:, :, np.newaxis]
        rel_velocities_lateral, rel_velocities_forward = mt.project_velocity(rel_velocities_x, rel_velocities_y, np.radians(angles))
        rel_velocities_mag = np.sqrt(rel_velocities_forward**2 + rel_velocities_lateral**2)

        list_relative = [
            dis, rel_angles, rel_orientation,
            rel_velocities_mag, rel_velocities_forward, rel_velocities_lateral
        ]

        rel_feature_names = [
            'distance', 'relative_angle', 'relative_orientation',
            'relative_velocity_mag', 'relative_velocity_forward', 'relative_velocity_lateral'
        ]

        relative = np.stack(list_relative, axis=3)
        ds_dict['rel_features'] = xr.DataArray(data=relative,
                                               dims=['time', 'flies', 'relative_flies', 'relative_features'],
                                               coords={'time': time,
                                                       'relative_features': rel_feature_names,
                                                       'nearest_frame': (('time'), nearest_frame)},
                                               attrs={'description': 'coords are "egocentric" - rel. to box',
                                                      'sampling_rate_Hz': sampling_rate,
                                                      'time_units': 'seconds',
                                                      'spatial_units': 'pixels'})

    # MAKE ONE DATASET
    feature_dataset = xr.Dataset(ds_dict, attrs={})
    return feature_dataset


def convert_spatial_units(ds: xr.Dataset, to_units: Optional[str] = None, names: Optional[List[str]] = None) -> xr.Dataset:
    """px <-> mm using attrs['pixels_to_mm'] and attrs['spatial_units'].

    Args:
        ds (xr.Dataset): xb Dataset to process
        to_units (Optional[str], optional): Target units - either 'pixels' or 'mm'. Defaults to None (always convert mm <-> px).
        names (Optional[List[str]], optional): Names of DataArrays/sets in ds to convert. Defaults to None.

    Returns:
        xr.Dataset: with converted spatial units
    """
    if names is None:
        names = ['body_positions', 'pose_positions', 'pose_positions_allo']

    for name in names:
        if name in ds:
            da = ds[name]
            spatial_units = da.attrs['spatial_units']
            pixel_size_mm = da.attrs['pixel_size_mm']

            if pixel_size_mm is np.nan:  # can't convert so skip
                continue

            if to_units is not None and spatial_units == to_units:  # already in the correct units so skip
                continue

            if spatial_units == 'mm':  # convert to pixels
                da /= pixel_size_mm
                da.attrs['spatial_units'] = 'pixels'
            elif spatial_units == 'pixels':  # convert to mm
                da *= pixel_size_mm
                da.attrs['spatial_units'] = 'mm'
    return ds


def interp_duplicates(y: np.array) -> np.array:
    """Replace duplicates with linearly interpolated values.

    Args:
        y (np.array): array with duplicates

    Returns:
        np.array: array without duplicates
    """
    x = np.arange(len(y))

    uni_mask = np.zeros_like(y, dtype=bool)
    uni_mask[np.unique(y, return_index=True)[1]] = True

    interp = scipy.interpolate.interp1d(x[uni_mask], y[uni_mask], kind='linear', copy=False, fill_value="extrapolate")

    y[~uni_mask] = interp(x[~uni_mask])

    return y

def save(savepath, dataset):
    """[summary]

    Args:
        savepath ([type]): [description]
        dataset ([type]): [description]
    """
    # xarray can't save attrs with dict values.
    # Delete event_times prior to saving.
    # Should make event_times a sparse xr.DataArray in the future.
    if 'song_events' in dataset:
        if 'event_times' in dataset.song_events.attrs:
            del dataset.song_events.attrs['event_times']

    with zarr.ZipStore(savepath, mode='w') as zarr_store:
        # re-chunking does not seem to help with IO speed upon lazy loading
        chunks = dict(dataset.dims)
        chunks['time'] = 100_000
        chunks['sampletime'] = 100_000
        dataset = dataset.chunk(chunks)
        dataset.to_zarr(store=zarr_store, compute=True)


def _normalize_strings(dataset):
    """Ensure all keys in coords are proper (unicode?) python strings, not byte strings."""
    dn = dict()

    for key, val in dataset.coords.items():
        if val.dtype == 'S16':
            dataset[key] = [v.decode() for v in val.data]
    return dataset


def load(savepath, lazy: bool = False, normalize_strings: bool = True,
         use_temp: bool = False):
    """[summary]

    Args:
        savepath ([type]): [description]
        lazy (bool, optional): [description]. Defaults to True.
        normalize_strings (bool, optional): [description]. Defaults to True.
        use_temp (bool, optional): Unpack zip to temp file - potentially speeds up loading and allows overwriting existing zarr file.
                                   Defaults to True.
    Returns:
        [type]: [description]
    """
    zarr_store = zarr.ZipStore(savepath, mode='r')
    if use_temp:
        dest = zarr.TempStore()
        zarr.copy_store(zarr_store, dest)
        zarr_store.close()
        zarr_store = dest
    dataset = xr.open_zarr(zarr_store)
    if not lazy:
        dataset.load()
        zarr_store.close()

    if normalize_strings:
        dataset = _normalize_strings(dataset)
    logging.info(dataset)
    return dataset

def load_npz(filepath: str, dataset: Optional[str] = 'data'):
    import numpy as np
    logging.info(f"Loading data from NPZ file at {filepath}.")
    with np.load(filepath) as file:
        try:
            sampling_rate = file['samplerate']
        except KeyError:
            sampling_rate = None
        data = file[dataset]
    return data, sampling_rate

def load_npy(filepath: str, dataset: Optional[str] = None):
    import numpy as np
    logging.info(f"Loading data from NPY file {filepath}.")
    data = np.load(filepath)
    samplingrate = None
    return data, samplingrate

def load_audio(filepath: str, dataset: Optional[str] = None):
    import soundfile
    logging.info(f"Loading data from audio file at {filepath}.")
    data, sampling_rate = soundfile.read(filepath)
    return data, sampling_rate

def load_wav(filepath: str, dataset: Optional[str] = None):
    import scipy.io.wavfile
    samplerate, data = scipy.io.wavfile.read(filepath)
    data = data[:, np.newaxis] if data.ndim==1 else data  # adds singleton dim for single-channel wavs
    return data, samplerate

def load_h5(filepath: str, dataset: Optional[str] = 'samples'):
    import h5py
    logging.info(f"Loading dataset {dataset} from HDF5 file {filepath}.")
    with h5py.File(filepath, 'r') as f:
        data = f[dataset][:]
    samplingrate = None
    return data, samplingrate

# def zarr(filepath):
#     import zarr
#     logging.info(f"Loading zarr file {filepath}.")
#     xb.load()
#     samplingrate = None
#     return data, samplingrate

data_loaders = {'audio': load_audio, 'npy': load_npy, 'npz': load_npz, 'h5': load_h5, 'wav': load_wav}

def from_file(filepath: str, loader_name: str = 'audio', target_samplerate: Optional[float] = None,
              samplerate: Optional[float] = None, dataset: Optional[str] = None,
              event_names=[], event_categories=[], annotation_path: Optional[str] = None,
              audio_channels: Optional[List[int]] = None):
    # TODO merge with from_data

    data, samplerate_from_file = data_loaders[loader_name](filepath, dataset)

    if audio_channels is not None:
        # audio_channels = np.arange(16)
        data = data[:, audio_channels]

    if samplerate is None:
        samplerate = samplerate_from_file

    if data.ndim==1:
        logging.info("   Data is 1d so prolly single-channel audio - appending singleton dimension.")
        data = data[:, np.newaxis]

    if event_names is None:
        event_names = []

    if event_categories is None:
        event_categories = []

    if event_names and not event_categories:
        logging.info('No event_categories specified - defaulting to segments')
        event_categories = ['segment'] * len(event_names)

    if len(event_names) != len(event_categories):
        raise ValueError(f'event_names and event_categories need to have same length - have {len(event_names)} and {len(event_categories)}.')

    if target_samplerate is None:
        target_samplerate = samplerate

    dataset_data = dict()
    sampletime = np.arange(len(data)) / samplerate
    time = np.arange(sampletime[0], sampletime[-1], 1 / target_samplerate)

    song_raw = xr.DataArray(data=data,  # cut recording to match new grid
                        dims=['sampletime', 'channels'],
                        coords={'sampletime': sampletime, },
                        attrs={'description': 'Song signal merged across all recording channels.',
                               'sampling_rate_Hz': samplerate,
                               'time_units': 'seconds',
                               'amplitude_units': 'volts'})
    dataset_data['song_raw'] = song_raw

    event_times = annot.Events()
    if annotation_path is not None and os.path.exists(annotation_path):
        try:
            df = pd.read_csv(annotation_path)
            event_times = annot.Events.from_df(df)
        except Exception as e:
            logging.exception(e)

    if event_names is not None and event_categories is not None:
        for event_name, event_category in zip(event_names, event_categories):
            if event_name not in event_times:
                event_times.add_name(event_name, event_category)

    ds_eventtimes = event_times.to_dataset()
    dataset_data['event_names'] = ds_eventtimes.event_names
    dataset_data['event_times'] = ds_eventtimes.event_times

    # MAKE THE DATASET
    ds = xr.Dataset(dataset_data, attrs={})
    ds.coords['time'] = time

    # save command line args
    ds.attrs = {'video_filename': '',
                'datename': filepath,
                'root': '', 'dat_path': '', 'res_path': '',
                'sampling_rate_Hz': samplerate,
                'target_sampling_rate_Hz': target_samplerate}
    logging.info(ds)
    return ds
