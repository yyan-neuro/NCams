#!python3
# -*- coding: utf-8 -*-
"""
NCams Toolbox
Copyright 2019 Charles M Greenspon, Anton Sobinov
https://github.com/CMGreenspon/NCams

Functions related to triangulation of marker positions from multiple cameras.

For more details on the camera data structures and dicts, see help(ncams.camera_tools).
"""
import os
import re
import csv
import shutil
import multiprocessing
import functools
import ntpath

import glob
import numpy as np
from tqdm import tqdm
import cv2
from scipy.signal import medfilt
from astropy.convolution import convolve, Gaussian1DKernel
import yaml

import matplotlib
import matplotlib.pyplot as mpl_pp
from mpl_toolkits.mplot3d import Axes3D
from mpl_toolkits.mplot3d.art3d import Poly3DCollection, Line3DCollection
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas

from . import utils
from . import image_tools
from . import camera_io
from . import camera_tools


FIG = None
FIGNUM = None
AXS = None
SLIDER = None


def triangulate(camera_config, output_csv, calibration_config, pose_estimation_config,
                labeled_csv_path, threshold=0.9, method='full_rank',
                best_pair_n=2, num_frames_limit=None, iteration=None, undistorted_data=False,
                file_prefix=''):
    '''Triangulates points from multiple cameras and exports them into a csv.

    Arguments:
        camera_config {dict} -- see help(ncams.camera_tools). This function uses following keys:
            serials {list of numbers} -- list of camera serials.
            dicts {dict of 'camera_dict's} -- keys are serials, values are 'camera_dict'.
        output_csv {str} -- file to save the triangulated points into.
        calibration_config {dict} -- see help(ncams.camera_tools).
        pose_estimation_config {dict} -- see help(ncams.camera_tools).
        labeled_csv_path {str} -- locations of csv's with marked points.
    Keyword Arguments:
        threshold {number 0-1} -- only points with confidence (likelihood) above the threshold will
            be used for triangulation. (default: 0.9)
        method {'full_rank' or 'best_pair'} -- method for triangulation.
            full_rank: uses all available cameras
            best_pair: uses best 'best_pair_n' cameras to locate the point.
            (default: 'full_rank')
        best_pair_n {number} -- how many cameras to use when best_pair method is used. (default: 2)
        num_frames_limit {number or None} -- limit to the number of frames used for analysis. Useful
            for testing. If None, then all frames will be analyzed. (default: None)
        iteration {int} -- look for csv's with this iteration number. (default: {None})
        undistorted_data {bool} -- if the marker data was made on undistorted videos. (default:
            {False})
        file_prefix {string} -- prefix of the csv file to search for in the folder. (default: {''})
    Output:
        output_csv {str} -- location of the output csv with all triangulated points.
    '''
    cam_serials = camera_config['serials']
    cam_dicts = camera_config['dicts']

    camera_matrices = calibration_config['camera_matrices']
    if not undistorted_data:
        distortion_coefficients = calibration_config['distortion_coefficients']

    world_locations = pose_estimation_config['world_locations']
    world_orientations = pose_estimation_config['world_orientations']

    # Get data files
    list_of_csvs = []
    for cam_serial in cam_serials:
        if iteration is None:
            sstr = '*.csv'
        else:
            sstr = '*_{}.csv'.format(iteration)
        list_of_csvs += glob.glob(os.path.join(
            labeled_csv_path, file_prefix+'*'+ cam_dicts[cam_serial]['name']+sstr))
    if not len(list_of_csvs) == len(cam_serials):
        if iteration is not None:
            raise ValueError('Detected {} csvs in {} with iteration #{} while was provided with {}'
                  ' serials.'.format(
                len(list_of_csvs), labeled_csv_path, iteration, len(cam_serials)))
        iterations = set()
        for csv_f in list_of_csvs:
            iterations.add(int(re.search('_[0-9]+.csv$', csv_f)[0][1:-4]))
        print('Detected {} csvs in {} while was provided with {} serials.'
              ' Found iterations: {}'.format(
            len(list_of_csvs), labeled_csv_path, len(cam_serials), sorted(iterations)))

        uinput_string = ('Provide iteration number to use: ')
        uinput = input(uinput_string)
        list_of_csvs = [i for i in list_of_csvs if re.fullmatch('.*_{}.csv'.format(uinput), i)]
        if not len(list_of_csvs) == len(cam_serials):
            raise ValueError('Detected {} csvs in {} with iteration #{} while was provided with {}'
                  ' serials.'.format(
                len(list_of_csvs), labeled_csv_path, uinput, len(cam_serials)))

    # Load them
    csv_arrays = [[] for _ in list_of_csvs]
    for ifile, csvfname in enumerate(list_of_csvs):
        with open(csvfname) as csvfile:
            reader_object = csv.reader(csvfile, delimiter=',')
            for row in reader_object:
                csv_arrays[ifile].append(row)

    # Get the list of bodyparts - this way doesn't require loading in a yaml though that might be
    # better for skeleton stuff
    temp_bodyparts = csv_arrays[0][1]
    bodypart_idx = np.arange(1, len(temp_bodyparts)-2, 3)
    bodyparts = []
    for idx in bodypart_idx:
        bodyparts.append(temp_bodyparts[idx])

    # Format the data
    num_cameras = len(csv_arrays)
    num_bodyparts = len(bodyparts)
    num_frames = len(csv_arrays[0])-3
    if num_frames_limit is not None and num_frames > num_frames_limit:
        num_frames = num_frames_limit

    image_coordinates, thresholds = [], []
    for icam in range(num_cameras):
        # Get the numerical data
        csv_array = np.vstack(csv_arrays[icam][3:])
        csv_array = csv_array[:num_frames, 1:]
        # Get coordinate and confidence idxs
        if icam == 0:
            confidence_idx = np.arange(2, np.shape(csv_array)[1], 3)
            coordinate_idx = []
            for idx in range(np.shape(csv_array)[1]):
                if not np.any(confidence_idx == idx):
                    coordinate_idx.append(idx)

        # Separate arrays
        coordinate_array = csv_array[:, coordinate_idx]
        threshold_array = csv_array[:, confidence_idx]

        # Format the coordinate array
        formatted_coordinate_array = np.empty((num_frames, 2, num_bodyparts))
        for ibp in range(num_bodyparts):
            formatted_coordinate_array[:, :, ibp] = coordinate_array[:, [ibp*2, ibp*2+1]]

        # Append to output lists
        image_coordinates.append(formatted_coordinate_array)
        thresholds.append(threshold_array)

    # Undistort the points and then threshold
    output_coordinates_filtered = []
    for icam in range(num_cameras):
        # Get the optimal camera matrix
        if not undistorted_data:
            optimal_matrix, _ = cv2.getOptimalNewCameraMatrix(
                camera_matrices[icam],
                distortion_coefficients[icam],
                (camera_config['image_size'][1], camera_config['image_size'][0]),
                1,
                (camera_config['image_size'][1], camera_config['image_size'][0]))

        # output_array = np.empty((num_frames, 2, num_bodyparts))
        filtered_output_array = np.empty((num_frames, 2, num_bodyparts))
        # The filtered one needs NaN points so we know which to ignore
        filtered_output_array.fill(np.nan)

        # Get the sufficiently confident values for each bodypart
        for bodypart in range(num_bodyparts):
            # Get the distorted points
            distorted_points = image_coordinates[icam][:, :, bodypart]
            if undistorted_data:
                undistorted_points = distorted_points.reshape(distorted_points.shape[0],1,2)
            else: # Undistort them
                undistorted_points = cv2.undistortPoints(
                    distorted_points, camera_matrices[icam],
                    distortion_coefficients[icam], P=optimal_matrix)

            # Get threshold filter
            bp_thresh = thresholds[icam][:, bodypart].astype(np.float32) > threshold
            thresh_idx = np.where(bp_thresh == 1)[0]
            # Put them into the output array
            for idx in thresh_idx:
                filtered_output_array[idx, :, bodypart] = undistorted_points[idx, 0, :]

        # output_coordinates.append(output_array)
        output_coordinates_filtered.append(filtered_output_array)

    # Triangulation
    # Make the projection matrices
    projection_matrices = []
    for icam in range(num_cameras):
        projection_matrices.append(camera_tools.make_projection_matrix(
            camera_matrices[icam], world_orientations[icam], world_locations[icam]))

    # Triangulate the points
    triangulated_points = np.empty((num_frames, 3, num_bodyparts))
    triangulated_points.fill(np.nan)

    for iframe in range(num_frames):
        for bodypart in range(num_bodyparts):
            # Get points for each camera
            cam_image_points = np.empty((2, num_cameras))
            cam_image_points.fill(np.nan)
            if method == 'full_rank' or (method == 'best_pair' and num_cameras <= best_pair_n):
                for icam in range(num_cameras):
                    cam_image_points[:, icam] = output_coordinates_filtered[icam][iframe, :, bodypart]
            elif method == 'best_pair':
                # decorate-sort-undecorate sort to find the icams for the highest likelihood
                best_likelh = [b[0] for b in sorted(
                    zip(range(num_cameras),
                        [thresholds[icam][iframe, bodypart].astype(np.float64)
                         for icam in range(num_cameras)]),
                    key=lambda x: x[1], reverse=True)][:best_pair_n]
                for icam in [icam for icam in range(num_cameras) if icam in best_likelh]:
                    cam_image_points[:, icam] = output_coordinates_filtered[icam][iframe, :, bodypart]

            # Check how many cameras detected the bodypart in that frame
            cams_detecting = ~np.isnan(cam_image_points[0, :])
            cam_idx = np.where(cams_detecting)[0]
            if np.sum(cams_detecting) < 2:
                continue

            # Perform the triangulation
            decomp_matrix = np.empty((np.sum(cams_detecting)*2, 4))
            for decomp_idx, cam in enumerate(cam_idx):
                point_mat = cam_image_points[:, cam]
                projection_mat = projection_matrices[cam]

                temp_decomp = np.vstack([
                    [point_mat[0] * projection_mat[2, :] - projection_mat[0, :]],
                    [point_mat[1] * projection_mat[2, :] - projection_mat[1, :]]])

                decomp_matrix[decomp_idx*2:decomp_idx*2 + 2, :] = temp_decomp

            Q = decomp_matrix.T.dot(decomp_matrix)
            u, _, _ = np.linalg.svd(Q)
            u = u[:, -1, np.newaxis]
            u_euclid = (u/u[-1, :])[0:-1, :]
            triangulated_points[iframe, :, bodypart] = np.transpose(u_euclid)

    with open(output_csv, 'w', newline='') as f:
        triagwriter = csv.writer(f)
        bps_line = ['bodyparts']
        for bp in bodyparts:
            bps_line += [bp]*3
        triagwriter.writerow(bps_line)
        triagwriter.writerow(['coords'] + ['x', 'y', 'z']*num_bodyparts)
        for iframe in range(num_frames):
            rw = [iframe]
            for ibp in range(num_bodyparts):
                rw += [triangulated_points[iframe, 0, ibp],
                       triangulated_points[iframe, 1, ibp],
                       triangulated_points[iframe, 2, ibp]]
            triagwriter.writerow(rw)
    return output_csv


def process_triangulated_data(csv_path, filt_width=5, interps=3, outlier_sd_threshold=5,
                              output_csv=None):
    '''Uses median and gaussian filters to both smooth and interpolate points.
       Will only interpolate when fewer missing values are present than the gaussian width.
       Arguments:
        csv_path {str} -- path of the triangulated csv.
    Keyword Arguments:
        filt_width {int} -- how wide the filters should be. (default: 5)
        output_csv {str} -- filename for the output smoothed csv. (default: {csv_path +
            _smoothed.csv})

    '''
    # Load in the CSV
    with open(csv_path, 'r') as f:
        triagreader = csv.reader(f)
        l = next(triagreader)
        bodyparts = []
        for i, bp in enumerate(l):
            if (i-1)%3 == 0:
                bodyparts.append(bp)
        num_bodyparts = len(bodyparts)
        next(triagreader)
        triangulated_points = []
        for row in triagreader:
            triangulated_points.append([[] for _ in range(3)])
            for ibp in range(num_bodyparts):
                triangulated_points[-1][0].append(float(row[1+ibp*3]))
                triangulated_points[-1][1].append(float(row[2+ibp*3]))
                triangulated_points[-1][2].append(float(row[3+ibp*3]))

    processed_array = np.array(triangulated_points)

    # Smooth each bodypart along each axis
    gauss_filt = Gaussian1DKernel(stddev=filt_width/10)

    for ibp in range(num_bodyparts):
        for a in range(3):
            ibp_a = np.squeeze(processed_array[:,a,ibp])
            # Apply median filter
            ibp_a = medfilt(ibp_a, kernel_size=filt_width)
            # Outlier detection
            mean_val = np.nanmean(ibp_a)
            std_val = np.nanstd(ibp_a)
            ut = mean_val + std_val*outlier_sd_threshold
            lt = mean_val - std_val*outlier_sd_threshold
            ibp_a = [np.nan if e > ut or e < lt else e for e in ibp_a]
            # Apply gaussian smoothing filter
            processed_array[:,a,ibp] = convolve(ibp_a, gauss_filt,boundary='extend')

    if output_csv is None:
        output_csv = csv_path[:-4] + '_smoothed.csv'

    with open(output_csv, 'w', newline='') as f:
        triagwriter = csv.writer(f)
        bps_line = ['bodyparts']
        for bp in bodyparts:
            bps_line += [bp]*3
        triagwriter.writerow(bps_line)
        triagwriter.writerow(['coords'] + ['x', 'y', 'z']*num_bodyparts)
        for iframe in range(processed_array.shape[0]):
            rw = [iframe]
            for ibp in range(num_bodyparts):
                rw += [processed_array[iframe, 0, ibp],
                       processed_array[iframe, 1, ibp],
                       processed_array[iframe, 2, ibp]]
            triagwriter.writerow(rw)

    return output_csv
'''
fig = mpl_pp.figure(figsize=(9, 5))
fig.add_subplot(1,2,1)
mpl_pp.plot(ibp_a)
fig.add_subplot(1,2,2)
mpl_pp.plot(test)
'''


def make_triangulation_videos(camera_config, cam_serials_to_use, video_paths, triangulated_csv_path,
                              skeleton_config=None, marker_size=5, output_path=None,
                              frame_range=None, parallel=None, view=(90, 90)):
    '''Makes a video based on triangulated marker positions.

    Arguments:
        camera_config {dict} -- see help(ncams.camera_tools). This function uses following keys:
            serials {list of numbers} -- list of camera serials.
            dicts {dict of 'camera_dict's} -- keys are serials, values are 'camera_dict'.
        cam_serials_to_use {list} -- list of serials for the cameras you want videos for.
        video_paths {list} -- list of undistorted videos. For each cam_serials_to_use, the function
            will choose the video filename that has the serial number within it.
        triangulated_csv_path {str} -- location of csv with triangulated points.
    Keyword Arguments:
        output_path {None, list or str} -- list of filenames corresponding to the 'video_paths'
            where the 3d video are going to be stored. If str, will store into that file. If string
            and multiple cam_serials_to_use, it will OVERWRITE the video each cam_serial. If None,
            will put the new videos into the directory of triangulated_csv_path. (default: None)
        frame_range {tuple or None} --  part of video and points to create a video for. If a tuple
            then indicates the start and stop frame. If None then all frames will be used. (default:
                None)
        skeleton_config {str} -- Path to yaml file with both 'bodyparts' and 'skeleton' as shown in
            the example config. (default: None)
        parallel {int or None} -- if not None, specifies number of processes to spawn for a pool. If
            None, then no parallelization. (default: None)
        view {tuple} -- The desired (elivation, azimuth) required for the 3d plot. (default:
            (90, 90))

    '''
    cam_dicts = camera_config['dicts']

    if isinstance(video_paths, str): # just in case a string is passed
        video_paths = [video_paths]

    if skeleton_config is not None:
        with open(skeleton_config, 'r') as yaml_file:
            dic = yaml.safe_load(yaml_file)
            bp_list = dic['bodyparts']
            bp_connections = dic['skeleton']
        skeleton = True
    else:
        skeleton = False

    with open(triangulated_csv_path, 'r') as f:
        triagreader = csv.reader(f)
        l = next(triagreader)
        bodyparts = []
        for i, bp in enumerate(l):
            if (i-1)%3 == 0:
                bodyparts.append(bp)
        num_bodyparts = len(bodyparts)
        next(triagreader)
        triangulated_points = []
        for row in triagreader:
            triangulated_points.append([[] for _ in range(3)])
            for ibp in range(num_bodyparts):
                triangulated_points[-1][0].append(float(row[1+ibp*3]))
                triangulated_points[-1][1].append(float(row[2+ibp*3]))
                triangulated_points[-1][2].append(float(row[3+ibp*3]))

    triangulated_points = np.array(triangulated_points)

    cmap = matplotlib.cm.get_cmap('jet')
    color_idx = np.linspace(0, 1, num_bodyparts)
    bp_cmap = cmap(color_idx)
    # Limits in space of the markers + 10%
    margin = 1.3
    pcntl = 2
    x_range = (np.nanpercentile(triangulated_points[:, 0, :], pcntl) * margin,
               np.nanpercentile(triangulated_points[:, 0, :], 100-pcntl) * margin)
    y_range = (np.nanpercentile(triangulated_points[:, 1, :], pcntl) * margin,
               np.nanpercentile(triangulated_points[:, 1, :], 100-pcntl) * margin)
    z_range = (np.nanpercentile(triangulated_points[:, 2, :], pcntl) * margin,
               np.nanpercentile(triangulated_points[:, 2, :], 100-pcntl) * margin)

    for cam_serial in cam_serials_to_use:
        print('Creating video for {}'.format(cam_dicts[cam_serial]['name']))

        # Get the video
        vid_path = [fn for fn in video_paths if str(cam_serial) in fn]
        if len(vid_path) == 0:
            raise ValueError('No videos detected matching the camera serial [{}], please inspect'
                             ' paths.'.format(cam_serial))
        if len(vid_path) > 1:
            raise ValueError('More than one video detected matching the camera serial [{}], please'
                             ' inspect paths.'.format(cam_serial))
        vid_path = vid_path[0]
        vid_id = video_paths.index(vid_path)

        # Inspect the video
        video = cv2.VideoCapture(vid_path)
        fps = int(video.get(cv2.CAP_PROP_FPS))
        num_frames = int(video.get(cv2.CAP_PROP_FRAME_COUNT))

        # Check that the number of frames matches the CSV
        # if not num_frames == np.size(triangulated_points, 0):
        #     print('   Warning: the CSV and video do not have an equal number of frames.')
        #     print(str(num_frames) + ' frames')
        #     print(str(np.size(triangulated_points, 0)) + ' rows')

        if output_path is None: # Use the same directory as the input CSV
            output_filename = (triangulated_csv_path[:-4] + '_' + ntpath.basename(vid_path)[:-4] +
                               '_triangulated.mp4')
        else:
            if isinstance(output_path, (list, tuple)):
                output_filename = output_path[vid_id]
            else:
                output_filename = output_path
                if len(cam_serials_to_use) > 1:
                    raise ValueError('Multiple camera serials provided, but only one output_path.')
        print('Making video into {}'.format(output_filename))

        # Check the frame range
        if frame_range is not None:
            if frame_range[1] > num_frames:
                print('   Too many frames requested, the video will be truncated appropriately.\n')
                frame_range[1] = num_frames
                video.set(cv2.CAP_PROP_POS_FRAMES, frame_range[0]) # Set the start position
        else:
            frame_range = (0, num_frames)

        # Create the figure
        fig = mpl_pp.figure(figsize=(9, 5))
        fw, fh = fig.get_size_inches() * fig.get_dpi()
        canvas = FigureCanvas(fig)
        # Make a new video keeping the old properties - need to know figure size first
        fourcc = cv2.VideoWriter_fourcc(*'MPEG')
        output_video = cv2.VideoWriter(output_filename, fourcc, fps, (int(fw), int(fh)))
        # Create the axes
        ax1 = fig.add_subplot(1, 2, 1)
        ax2 = fig.add_subplot(1, 2, 2, projection='3d')
        ax2.view_init(elev=view[0], azim=view[1])

        for f_idx in tqdm(range(frame_range[0], frame_range[1], 1)):
            fe, frame = video.read() # Read the next frame
            if fe is False:
                break

            frame_rgb = frame[...,::-1].copy()
            # Clear axis 1
            ax1.cla()
            ax1.imshow(frame_rgb)
            ax1.set_xticks([])
            ax1.set_yticks([])
            # Clear axis 2
            ax2.cla()
            ax2.set_xlim(x_range)
            ax2.set_ylim(y_range)
            ax2.set_zlim(z_range)

            # Underlying skeleton
            if skeleton:
                for isk in range(len(bp_connections)):
                    ibp1 = bp_list.index(bp_connections[isk][0])
                    ibp2 = bp_list.index(bp_connections[isk][1])

                    t_point1 = triangulated_points[f_idx, :, ibp1]
                    t_point2 = triangulated_points[f_idx, :, ibp2]

                    if any(np.isnan(t_point1)) or any(np.isnan(t_point1)):
                        continue
                    else:
                        ax2.plot([t_point1[0], t_point2[0]],
                                 [t_point1[1], t_point2[1]],
                                 [t_point1[2], t_point2[2]],
                                 color='k',linewidth=1)

            # Bodypart markers
            for ibp in range(np.size(triangulated_points, 2)):
                # Markers
                ax2.scatter(triangulated_points[f_idx, 0, ibp],
                            triangulated_points[f_idx, 1, ibp],
                            triangulated_points[f_idx, 2, ibp],
                            color=bp_cmap[ibp, :], s=marker_size)

            # Pull matplotlib data to a variable and format for writing
            canvas.draw()
            temp_frame = np.fromstring(canvas.tostring_rgb(), dtype='uint8').reshape(int(fh), int(fw), 3)
            temp_frame = temp_frame[...,::-1].copy()
            output_video.write(temp_frame)

        # Release objects
        mpl_pp.close(fig)
        video.release()
        output_video.release()

        print('*  Video saved to:\n' + '   ' + output_filename)


def _make_triangulation_video():
    '''Use for parallelization of make_triangulation_videos

    Not Implemented - need to figure out what to do about dialogue.

    [description]
    '''
    raise NotImplementedError
    pass


def make_image(args, ranges=None, output_path=None, bp_cmap=None):
    iframe, image_path, triangulated_points = args
    if output_path is None:
        output_path = os.getcwd()

    fig = mpl_pp.figure(figsize=(9, 5))
    ax1 = fig.add_subplot(1, 2, 1)
    # Create the figure
    ax1.imshow(mpl_pp.imread(image_path))

    ax2 = fig.add_subplot(1, 2, 2, projection='3d')
    ax2.view_init(elev=90, azim=90)
    if ranges is not None:
        ax2.set_xlim(ranges[0])
        ax2.set_ylim(ranges[1])
        ax2.set_zlim(ranges[2])

    if bp_cmap is None:
        for ibp in range(np.size(triangulated_points, 1)):
            ax2.scatter(triangulated_points[0, ibp],
                        triangulated_points[1, ibp],
                        triangulated_points[2, ibp])
    else:
        for ibp in range(np.size(triangulated_points, 1)):
            ax2.scatter(triangulated_points[0, ibp],
                        triangulated_points[1, ibp],
                        triangulated_points[2, ibp],
                        color=bp_cmap[ibp, :])

    mpl_pp.savefig(os.path.join(output_path, 'frame' + str(iframe)))
    mpl_pp.close(fig)


def interactive_3d_plot(cam_serial, camera_config, session_config, triangulated_csv,
                        num_frames_limit=None):
    """Makes an interactive 3D plot with video and a slider to control the frame number.

    Arguments:
        cam_serial {int} -- camera serial of the camera to plot.
        camera_config {dict} -- see help(ncams.camera_tools). This function uses following keys:
            serials {list of numbers} -- list of camera serials.
            dicts {dict of 'camera_dict's} -- keys are serials, values are 'camera_dict'.
        session_config {dict} -- information about the session. This function uses following keys:
            session_path {str} -- location of the session data.
        calibration_config {dict} -- see help(ncams.camera_tools).
        pose_estimation_config {dict} -- see help(ncams.camera_tools).
        triangulated_csv {str} -- location of csv with marked points.
    Keyword Arguments:
        overwrite_temp {bool} -- automatically overwrite folder for holding temporary images.
            (default: {False})
        fps {number} -- for making movies. (default: {30})
        num_frames_limit {number or None} -- limit to the number of frames used for analysis. Useful
            for testing. If None, then all frames will be analyzed. (default: None)
        parallel {number or None} parallelize the image creation. If integer, create that many
            processes. Significantly speeds up generation. If None, do not parallelize. (default:
            {None})
    """
    raise DeprecationWarning
    cam_dicts = camera_config['dicts']
    session_path = session_config['session_path']

    with open(triangulated_csv, 'r') as f:
        triagreader = csv.reader(f)
        l = next(triagreader)
        bodyparts = []
        for i, bp in enumerate(l):
            if (i-1)%3 == 0:
                bodyparts.append(bp)
        num_bodyparts = len(bodyparts)
        next(triagreader)
        triangulated_points = []
        num_frames = 0
        for row in triagreader:
            triangulated_points.append([[] for _ in range(3)])
            for ibp in range(num_bodyparts):
                triangulated_points[-1][0].append(float(row[1+ibp*3]))
                triangulated_points[-1][1].append(float(row[2+ibp*3]))
                triangulated_points[-1][2].append(float(row[3+ibp*3]))
            num_frames += 1
            if num_frames_limit is not None and num_frames >= num_frames_limit:
                break
    triangulated_points = np.array(triangulated_points)

    image_list = utils.get_image_list(path=os.path.join(
        session_path, cam_dicts[cam_serial]['name']))

    cmap = matplotlib.cm.get_cmap('jet')
    color_idx = np.linspace(0, 1, num_bodyparts)
    bp_cmap = cmap(color_idx)
    # Limits in space of the markers + 10%
    margin = 1.3
    pcntl = 2
    x_range = (np.nanpercentile(triangulated_points[:, 0, :], pcntl) * margin,
               np.nanpercentile(triangulated_points[:, 0, :], 100-pcntl) * margin)
    y_range = (np.nanpercentile(triangulated_points[:, 1, :], pcntl) * margin,
               np.nanpercentile(triangulated_points[:, 1, :], 100-pcntl) * margin)
    z_range = (np.nanpercentile(triangulated_points[:, 2, :], pcntl) * margin,
               np.nanpercentile(triangulated_points[:, 2, :], 100-pcntl) * margin)

    global FIG, FIGNUM, AXS, SLIDER

    FIG = mpl_pp.figure(figsize=(9, 5))
    FIGNUM = mpl_pp.gcf().number
    AXS = []
    AXS.append(FIG.add_subplot(1, 2, 1))
    AXS.append(FIG.add_subplot(1, 2, 2, projection='3d'))
    AXS[1].view_init(elev=90, azim=90)

    def update(iframe):
        mpl_pp.figure(FIGNUM)
        AXS[0].cla()
        image_path = image_list[int(iframe)]
        AXS[0].imshow(mpl_pp.imread(image_path))

        AXS[1].cla()
        AXS[1].set_xlim(x_range)
        AXS[1].set_ylim(y_range)
        AXS[1].set_zlim(z_range)

        for ibp in range(np.size(triangulated_points, 2)):
            AXS[1].scatter(triangulated_points[int(iframe), 0, ibp],
                           triangulated_points[int(iframe), 1, ibp],
                           triangulated_points[int(iframe), 2, ibp],
                           color=bp_cmap[ibp, :])
    update(0)

    axcolor = 'lightgoldenrodyellow'
    ax_ind = mpl_pp.axes([0.15, 0.1, 0.65, 0.03], facecolor=axcolor)
    SLIDER = mpl_pp.Slider(ax_ind, 'Ind', 0, num_frames-1, valinit=0)
    SLIDER.on_changed(update)

    mpl_pp.show()
