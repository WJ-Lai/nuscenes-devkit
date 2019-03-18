# nuScenes dev-kit.
# Code written by Oscar Beijbom, 2018.
# Licensed under the Creative Commons [see licence.txt]

from __future__ import annotations

import json
import time
import sys
import os.path as osp
from datetime import datetime
from typing import Tuple, List

import cv2
import numpy as np
import matplotlib.pyplot as plt
from PIL import Image
from matplotlib.axes import Axes
from pyquaternion import Quaternion
import sklearn.metrics
from tqdm import tqdm

from nuscenes.utils.map_mask import MapMask
from nuscenes.utils.data_classes import LidarPointCloud, RadarPointCloud, Box
from nuscenes.utils.geometry_utils import view_points, box_in_image, quaternion_slerp, BoxVisibility


PYTHON_VERSION = sys.version_info[0]

if not PYTHON_VERSION == 3:
    raise ValueError("nuScenes dev-kit only supports Python version 3.")


class NuScenes:
    """
    Database class for nuScenes to help query and retrieve information from the database.
    """

    def __init__(self, version: str='v0.1', dataroot: str='/data/nuscenes', verbose: bool=True):
        """
        Loads database and creates reverse indexes and shortcuts.
        :param version: Version to load (e.g. "v0.1", ...).
        :param dataroot: Path to the tables and data.
        :param verbose: Whether to print status messages during load.
        """
        if version not in ['v0.1']:
            raise ValueError('Invalid DB version: {}'.format(version))

        self.version = version
        self.dataroot = dataroot
        self.table_names = ['category', 'attribute', 'visibility', 'instance', 'sensor', 'calibrated_sensor',
                            'ego_pose', 'log', 'scene', 'sample', 'sample_data', 'sample_annotation', 'map']

        assert osp.exists(self.table_root), 'Database version not found: {}'.format(self.table_root)

        start_time = time.time()
        if verbose:
            print("======\nLoading NuScenes tables for version {} ...".format(self.version))

        # Explicitly assign tables to help the IDE determine valid class members.
        self.category = self.__load_table__('category')
        self.attribute = self.__load_table__('attribute')
        self.visibility = self.__load_table__('visibility')
        self.instance = self.__load_table__('instance')
        self.sensor = self.__load_table__('sensor')
        self.calibrated_sensor = self.__load_table__('calibrated_sensor')
        self.ego_pose = self.__load_table__('ego_pose')
        self.log = self.__load_table__('log')
        self.scene = self.__load_table__('scene')
        self.sample = self.__load_table__('sample')
        self.sample_data = self.__load_table__('sample_data')
        self.sample_annotation = self.__load_table__('sample_annotation')
        self.map = self.__load_table__('map')

        # Initialize map mask for each map record.
        for map_record in self.map:
            map_record['mask'] = MapMask(osp.join(self.dataroot, map_record['filename']))

        if verbose:
            for table in self.table_names:
                print("{} {},".format(len(getattr(self, table)), table))
            print("Done loading in {:.1f} seconds.\n======".format(time.time() - start_time))

        # Make reverse indexes for common lookups.
        self.__make_reverse_index__(verbose)

        # Initialize NuScenesExplorer class
        self.explorer = NuScenesExplorer(self)

    @property
    def table_root(self) -> str:
        """ Returns the folder where the tables are stored for the relevant version. """
        return osp.join(self.dataroot, self.version)

    def __load_table__(self, table_name) -> dict:
        """ Loads a table. """
        with open(osp.join(self.table_root, '{}.json'.format(table_name))) as f:
            table = json.load(f)
        return table

    def __make_reverse_index__(self, verbose: bool) -> None:
        """
        De-normalizes database to create reverse indices for common cases.
        :param verbose: Whether to print outputs.
        """

        start_time = time.time()
        if verbose:
            print("Reverse indexing ...")

        # Store the mapping from token to table index for each table.
        self._token2ind = dict()
        for table in self.table_names:
            self._token2ind[table] = dict()

            for ind, member in enumerate(getattr(self, table)):
                self._token2ind[table][member['token']] = ind

        # Decorate (adds short-cut) sample_annotation table with for category name.
        for record in self.sample_annotation:
            inst = self.get('instance', record['instance_token'])
            record['category_name'] = self.get('category', inst['category_token'])['name']

        # Decorate (adds short-cut) sample_data with sensor information.
        for record in self.sample_data:
            cs_record = self.get('calibrated_sensor', record['calibrated_sensor_token'])
            sensor_record = self.get('sensor', cs_record['sensor_token'])
            record['sensor_modality'] = sensor_record['modality']
            record['channel'] = sensor_record['channel']

        # Reverse-index samples with sample_data and annotations.
        for record in self.sample:
            record['data'] = {}
            record['anns'] = []

        for record in self.sample_data:
            if record['is_key_frame']:
                sample_record = self.get('sample', record['sample_token'])
                sample_record['data'][record['channel']] = record['token']

        for ann_record in self.sample_annotation:
            sample_record = self.get('sample', ann_record['sample_token'])
            sample_record['anns'].append(ann_record['token'])

        # Add reverse indices from log records to map records.
        for log_record in self.log:
            map_token = self.field2token('map', 'log_token', log_record['token'])[0]
            log_record['map_token'] = self.get('map', map_token)['token']

        if verbose:
            print("Done reverse indexing in {:.1f} seconds.\n======".format(time.time() - start_time))
    
    def get(self, table_name: str, token: str) -> dict:
        """
        Returns a record from table in constant runtime.
        :param table_name: Table name.
        :param token: Token of the record.
        :return: Table record. See README.md for record details for each table.
        """
        assert table_name in self.table_names, "Table {} not found".format(table_name)

        return getattr(self, table_name)[self.getind(table_name, token)]

    def getind(self, table_name: str, token: str) -> int:
        """
        This returns the index of the record in a table in constant runtime.
        :param table_name: Table name.
        :param token: Token of the record.
        :return: The index of the record in table, table is an array.
        """
        return self._token2ind[table_name][token]

    def field2token(self, table_name: str, field: str, query) -> List[str]:
        """
        This function queries all record for a certain field value, and returns the tokens for the matching records.
        Warning: this runs in linear time.
        :param table_name: Table name.
        :param field: Field name. See README.md for details.
        :param query: Query to match against. Needs to type match the content of the query field.
        :return: List of tokens for the matching records.
        """
        matches = []
        for member in getattr(self, table_name):
            if member[field] == query:
                matches.append(member['token'])
        return matches

    def get_sample_data_path(self, sample_data_token: str) -> str:
        """ Returns the path to a sample_data. """

        sd_record = self.get('sample_data', sample_data_token)
        return osp.join(self.dataroot, sd_record['filename'])

    def get_sample_data(self, sample_data_token, box_vis_level=BoxVisibility.ANY, selected_anntokens=None) -> \
            Tuple[str, List[Box], np.array]:
        """
        Returns the data path as well as all annotations related to that sample_data.
        Note that the boxes are transformed into the current sensor's coordinate frame.
        :param sample_data_token: <str>. Sample_data token.
        :param box_vis_level: <BoxVisibility>. If sample_data is an image, this sets required visibility for boxes.
        :param selected_anntokens: [<str>]. If provided only return the selected annotation.
        :return: (data_path <str>, boxes [<Box>], camera_intrinsic <np.array: 3, 3>)
        """

        # Retrieve sensor & pose records
        sd_record = self.get('sample_data', sample_data_token)
        cs_record = self.get('calibrated_sensor', sd_record['calibrated_sensor_token'])
        sensor_record = self.get('sensor', cs_record['sensor_token'])
        pose_record = self.get('ego_pose', sd_record['ego_pose_token'])

        data_path = self.get_sample_data_path(sample_data_token)

        if sensor_record['modality'] == 'camera':
            cam_intrinsic = np.array(cs_record['camera_intrinsic'])
            imsize = (sd_record['width'], sd_record['height'])
        else:
            cam_intrinsic = None
            imsize = None

        # Retrieve all sample annotations and map to sensor coordinate system.
        if selected_anntokens is not None:
            boxes = list(map(self.get_box, selected_anntokens))
        else:
            boxes = self.get_boxes(sample_data_token)

        # Make list of Box objects including coord system transforms.
        box_list = []
        for box in boxes:

            # Move box to ego vehicle coord system
            box.translate(-np.array(pose_record['translation']))
            box.rotate(Quaternion(pose_record['rotation']).inverse)

            #  Move box to sensor coord system
            box.translate(-np.array(cs_record['translation']))
            box.rotate(Quaternion(cs_record['rotation']).inverse)

            if sensor_record['modality'] == 'camera' and not \
                    box_in_image(box, cam_intrinsic, imsize, vis_level=box_vis_level):
                continue

            box_list.append(box)

        return data_path, box_list, cam_intrinsic

    def get_box(self, sample_annotation_token: str) -> Box:
        """
        Instantiates a Box class from a sample annotation record.
        :param sample_annotation_token: Unique sample_annotation identifier.
        """
        record = self.get('sample_annotation', sample_annotation_token)
        return Box(record['translation'], record['size'], Quaternion(record['rotation']), name=record['category_name'])

    def get_boxes(self, sample_data_token: str) -> List[Box]:
        """
        Instantiates Boxes for all annotation for a particular sample_data record. If the sample_data is a
        keyframe, this returns the annotations for that sample. But if the sample_data is an intermediate
        sample_data, a linear interpolation is applied to estimate the location of the boxes at the time the
        sample_data was captured.
        :param sample_data_token: Unique sample_data identifier.
        """

        # Retrieve sensor & pose records
        sd_record = self.get('sample_data', sample_data_token)
        curr_sample_record = self.get('sample', sd_record['sample_token'])

        if curr_sample_record['prev'] == "" or sd_record['is_key_frame']:
            # If no previous annotations available, or if sample_data is keyframe just return the current ones.
            boxes = list(map(self.get_box, curr_sample_record['anns']))

        else:
            prev_sample_record = self.get('sample', curr_sample_record['prev'])

            curr_ann_recs = [self.get('sample_annotation', token) for token in curr_sample_record['anns']]
            prev_ann_recs = [self.get('sample_annotation', token) for token in prev_sample_record['anns']]

            # Maps instance tokens to prev_ann records
            prev_inst_map = {entry['instance_token']: entry for entry in prev_ann_recs}

            t0 = prev_sample_record['timestamp']
            t1 = curr_sample_record['timestamp']
            t = sd_record['timestamp']

            # There are rare situations where the timestamps in the DB are off so ensure that t0 < t < t1.
            t = max(t0, min(t1, t))

            boxes = []
            for curr_ann_rec in curr_ann_recs:

                if curr_ann_rec['instance_token'] in prev_inst_map:
                    # If the annotated instance existed in the previous frame, interpolate center & orientation.
                    prev_ann_rec = prev_inst_map[curr_ann_rec['instance_token']]

                    # Interpolate center.
                    center = [np.interp(t, [t0, t1], [c0, c1]) for c0, c1 in zip(prev_ann_rec['translation'],
                                                                                 curr_ann_rec['translation'])]

                    # Interpolate orientation. (There is a bug in pyquaternion.slerp() so use external method.)
                    rotation = Quaternion(quaternion_slerp(np.array(prev_ann_rec['rotation']),
                                                           np.array(curr_ann_rec['rotation']),
                                                           (t - t0) / (t1 - t0)))

                    box = Box(center, curr_ann_rec['size'], rotation, name=curr_ann_rec['category_name'])
                else:
                    # If not, simply grab the current annotation.
                    box = self.get_box(curr_ann_rec['token'])

                boxes.append(box)
        return boxes
    

    def box_velocity(self, sample_annotation_token: str, max_time_diff: float=1.5) -> np.ndarray:
        """
        Estimate the velocity for an annotation.
        If possible, we compute the centered difference between the previous and next frame.
        Otherwise we use the difference between the current and previous/next frame.
        If the velocity cannot be estimated, values are set to np.nan.
        :param sample_annotation_token: Unique sample_annotation identifier.
        :param max_time_diff: Max allowed time diff between consecutive samples that are used to estimate velocities.
        :return: <np.float: 3>. Velocity in x/y/z direction in m/s.
        """

        current = self.get('sample_annotation', sample_annotation_token)
        has_prev = current['prev'] != ''
        has_next = current['next'] != ''

        # Cannot estimate velocity for a single annotation.
        if not has_prev and not has_next:
            return np.array([np.nan, np.nan, np.nan])

        if has_prev:
            first = self.get('sample_annotation', current['prev'])
        else:
            first = current

        if has_next:
            last = self.get('sample_annotation', current['next'])
        else:
            last = current

        pos_last = np.array(last['translation'])
        pos_first = np.array(first['translation'])
        pos_diff = pos_last - pos_first

        time_last = 1e-6 * self.get('sample', last['sample_token'])['timestamp']
        time_first = 1e-6 * self.get('sample', first['sample_token'])['timestamp']
        time_diff = time_last - time_first

        if has_next and has_prev:
            # If doing centered difference, allow for up to double the max_time_diff.
            max_time_diff *= 2

        if time_diff > max_time_diff:
            # If time_diff is too big, don't return an estimate.
            return np.array([np.nan, np.nan, np.nan])
        else:
            return pos_diff / time_diff

    def list_categories(self) -> None:
        self.explorer.list_categories()

    def list_attributes(self) -> None:
        self.explorer.list_attributes()

    def list_scenes(self) -> None:
        self.explorer.list_scenes()

    def list_sample(self, sample_token: str) -> None:
        self.explorer.list_sample(sample_token)

    def render_pointcloud_in_image(self, sample_token: str, dot_size: int=5,  pointsensor_channel: str='LIDAR_TOP',
                                   camera_channel: str='CAM_FRONT') -> None:
        self.explorer.render_pointcloud_in_image(sample_token, dot_size, pointsensor_channel=pointsensor_channel,
                                                 camera_channel=camera_channel)

    def render_sample(self, sample_token: str, box_vis_level: BoxVisibility=BoxVisibility.ANY, nsweeps: int=1) -> None:
        self.explorer.render_sample(sample_token, box_vis_level, nsweeps=nsweeps)

    def render_sample_data(self, sample_data_token: str, with_anns: bool=True,
                           box_vis_level: BoxVisibility=BoxVisibility.ANY, axes_limit: float=40, ax: Axes=None,
                           nsweeps: int=1) -> None:
        self.explorer.render_sample_data(sample_data_token, with_anns, box_vis_level, axes_limit, ax, nsweeps=nsweeps)

    def render_annotation(self, sample_annotation_token: str, margin: float=10, view: np.ndarray=np.eye(4),
                          box_vis_level: BoxVisibility=BoxVisibility.ANY) -> None:
        self.explorer.render_annotation(sample_annotation_token, margin, view, box_vis_level)

    def render_instance(self, instance_token: str) -> None:
        self.explorer.render_instance(instance_token)

    def render_scene(self, scene_token: str, freq: float=10, imsize: Tuple[float, float]=(640, 360),
                     out_path: str=None) -> None:
        self.explorer.render_scene(scene_token, freq, imsize, out_path)

    def render_scene_channel(self, scene_token: str, channel: str='CAM_FRONT', imsize: Tuple[float, float]=(640, 360)):
        self.explorer.render_scene_channel(scene_token, channel=channel, imsize=imsize)

    def render_egoposes_on_map(self, log_location: str, scene_tokens: List=None, demo_ss_factor: float=2.0) -> None:
        self.explorer.render_egoposes_on_map(log_location, scene_tokens, demo_ss_factor)

    def save_sample_token(self, start_sample_token: str) -> List[str]:
        return self.explorer.save_sample_token(start_sample_token)

    def save_sample_data(self, start_sample_token: str, channel: str='RADAR_FRONT', with_anns: bool=True,
                           box_vis_level: BoxVisibility=BoxVisibility.ANY, axes_limit: float=40, ax: Axes=None,
                           nsweeps: int=1) -> None :
        self.explorer.save_sample_data(start_sample_token, channel, with_anns, 
                                       box_vis_level, axes_limit, ax, nsweeps=nsweeps)

    def render_pointcloud_channel(self) -> None:
        self.explorer.render_pointcloud_channel()

    def get_pointcloud_position(self, pointsensor_token: str, axes_limit: float=40) -> Tuple:
        points = self.explorer.get_pointcloud_position(pointsensor_token)
        return points
    
    def plot_pointcloud_prediction(self, obj_mean) -> None:
        self.plot_pointcloud_prediction(obj_mean)


class NuScenesExplorer:
    """ Helper class to list and visualize NuScenes data. These are meant to serve as tutorials and templates for
    working with the data. """

    def __init__(self, nusc: NuScenes):
        self.nusc = nusc

    @staticmethod
    def get_color(category_name: str) -> Tuple[int, int, int]:
        """ Provides the default colors based on the category names. """
        if category_name in ['vehicle.bicycle', 'vehicle.motorcycle']:
            return 255, 61, 99  # Red
        elif 'vehicle' in category_name:
            return 255, 158, 0  # Orange
        elif 'human.pedestrian' in category_name:
            return 0, 0, 230  # Blue
        elif 'cone' in category_name or 'barrier' in category_name:
            return 0, 0, 0  # Black
        else:
            return 255, 0, 255  # Magenta

    def list_categories(self) -> None:
        """ Print categories, counts and stats. """
        categories = dict()
        for record in self.nusc.sample_annotation:
            if record['category_name'] not in categories:
                categories[record['category_name']] = []
            categories[record['category_name']].append(record['size'] + [record['size'][1] / record['size'][0]])

        for name, stats in sorted(categories.items()):
            stats = np.array(stats)
            print('{:27} n={:5}, width={:5.2f}\u00B1{:.2f}, len={:5.2f}\u00B1{:.2f}, height={:5.2f}\u00B1{:.2f}, '
                  'lw_aspect={:5.2f}\u00B1{:.2f}'.format(name[:27], stats.shape[0],
                                                         np.mean(stats[:, 0]), np.std(stats[:, 0]),
                                                         np.mean(stats[:, 1]), np.std(stats[:, 1]),
                                                         np.mean(stats[:, 2]), np.std(stats[:, 2]),
                                                         np.mean(stats[:, 3]), np.std(stats[:, 3])))

    def list_attributes(self) -> None:
        """ Prints attributes and counts. """
        attribute_counts = dict()
        for record in self.nusc.sample_annotation:
            for attribute_token in record['attribute_tokens']:
                att_name = self.nusc.get('attribute', attribute_token)['name']
                if att_name not in attribute_counts:
                    attribute_counts[att_name] = 0
                attribute_counts[att_name] += 1

        for name, count in sorted(attribute_counts.items()):
            print('{}: {}'.format(name, count))

    def list_scenes(self) -> None:
        """ Lists all scenes with some meta data. """

        def ann_count(record):
            count = 0
            sample = self.nusc.get('sample', record['first_sample_token'])
            while not sample['next'] == "":
                count += len(sample['anns'])
                sample = self.nusc.get('sample', sample['next'])
            return count

        recs = [(self.nusc.get('sample', record['first_sample_token'])['timestamp'], record) for record in
                self.nusc.scene]

        for start_time, record in sorted(recs):
            start_time = self.nusc.get('sample', record['first_sample_token'])['timestamp'] / 1000000
            length_time = self.nusc.get('sample', record['last_sample_token'])['timestamp'] / 1000000 - start_time
            location = self.nusc.get('log', record['log_token'])['location']
            desc = record['name'] + ', ' + record['description']
            if len(desc) > 55:
                desc = desc[:51] + "..."

            print('{:16} [{}] {:4.0f}s, {}, #anns:{}'.format(
                desc, datetime.utcfromtimestamp(start_time).strftime('%y-%m-%d %H:%M:%S'),
                length_time, location, ann_count(record)))

    def list_sample(self, sample_token: str) -> None:
        """ Prints sample_data tokens and sample_annotation tokens related to the sample_token. """

        sample_record = self.nusc.get('sample', sample_token)
        print('Sample: {}\n'.format(sample_record['token']))
        for sd_token in sample_record['data'].values():
            sd_record = self.nusc.get('sample_data', sd_token)
            print('sample_data_token: {}, mod: {}, channel: {}'.format(sd_token, sd_record['sensor_modality'],
                                                                       sd_record['channel']))
        print('')
        for ann_token in sample_record['anns']:
            ann_record = self.nusc.get('sample_annotation', ann_token)
            print('sample_annotation_token: {}, category: {}'.format(ann_record['token'], ann_record['category_name']))

    def map_pointcloud_to_image(self, pointsensor_token: str, camera_token: str) -> Tuple:
        """
        Given a point sensor (lidar/radar) token and camera sample_data token, load point-cloud and map it to the image
        plane.
        :param pointsensor_token: Lidar/radar sample_data token.
        :param camera_token: Camera sample_data token.
        :return (pointcloud <np.float: 2, n)>, coloring <np.float: n>, image <Image>).
        """

        cam = self.nusc.get('sample_data', camera_token)
        pointsensor = self.nusc.get('sample_data', pointsensor_token)
        pcl_path = osp.join(self.nusc.dataroot, pointsensor['filename'])
        if pointsensor['sensor_modality'] == 'lidar':
            pc = LidarPointCloud.from_file(pcl_path)
        else:
            pc = RadarPointCloud.from_file(pcl_path)
        im = Image.open(osp.join(self.nusc.dataroot, cam['filename']))

        # Points live in the point sensor frame. So they need to be transformed via global to the image plane.
        # First step: transform the point-cloud to the ego vehicle frame for the timestamp of the sweep.
        cs_record = self.nusc.get('calibrated_sensor', pointsensor['calibrated_sensor_token'])
        pc.rotate(Quaternion(cs_record['rotation']).rotation_matrix)
        pc.translate(np.array(cs_record['translation']))

        # Second step: transform to the global frame.
        poserecord = self.nusc.get('ego_pose', pointsensor['ego_pose_token'])
        pc.rotate(Quaternion(poserecord['rotation']).rotation_matrix)
        pc.translate(np.array(poserecord['translation']))

        # Third step: transform into the ego vehicle frame for the timestamp of the image.
        poserecord = self.nusc.get('ego_pose', cam['ego_pose_token'])
        pc.translate(-np.array(poserecord['translation']))
        pc.rotate(Quaternion(poserecord['rotation']).rotation_matrix.T)

        # Fourth step: transform into the camera.
        cs_record = self.nusc.get('calibrated_sensor', cam['calibrated_sensor_token'])
        pc.translate(-np.array(cs_record['translation']))
        pc.rotate(Quaternion(cs_record['rotation']).rotation_matrix.T)

        # Fifth step: actually take a "picture" of the point cloud.
        # Grab the depths (camera frame z axis points away from the camera).
        depths = pc.points[2, :]

        # Set the height to be the coloring.
        coloring = pc.points[2, :]

        # Take the actual picture (matrix multiplication with camera-matrix + renormalization).
        points = view_points(pc.points[:3, :], np.array(cs_record['camera_intrinsic']), normalize=True)

        # Remove points that are either outside or behind the camera. Leave a margin of 1 pixel for aesthetic reasons.
        mask = np.ones(depths.shape[0], dtype=bool)
        mask = np.logical_and(mask, depths > 0)
        mask = np.logical_and(mask, points[0, :] > 1)
        mask = np.logical_and(mask, points[0, :] < im.size[0] - 1)
        mask = np.logical_and(mask, points[1, :] > 1)
        mask = np.logical_and(mask, points[1, :] < im.size[1] - 1)
        points = points[:, mask]
        coloring = coloring[mask]

        return points, coloring, im

    def render_pointcloud_in_image(self, sample_token: str, dot_size: int=5, pointsensor_channel: str='LIDAR_TOP',
                                   camera_channel: str='CAM_FRONT') -> None:
        """
        Scatter-plots a point-cloud on top of image.
        :param sample_token: Sample token.
        :param dot_size: Scatter plot dot size.
        :param pointsensor_channel: RADAR or LIDAR channel name, e.g. 'LIDAR_TOP'.
        :param camera_channel: Camera channel name, e.g. 'CAM_FRONT'.
        """
        sample_record = self.nusc.get('sample', sample_token)

        # Here we just grab the front camera and the point sensor.
        pointsensor_token = sample_record['data'][pointsensor_channel]
        camera_token = sample_record['data'][camera_channel]

        points, coloring, im = self.map_pointcloud_to_image(pointsensor_token, camera_token)
        plt.figure(figsize=(9, 16))
        plt.imshow(im)
        plt.scatter(points[0, :], points[1, :], c=coloring, s=dot_size)
        plt.axis('off')

    def render_sample(self, token: str, box_vis_level: BoxVisibility=BoxVisibility.ANY, nsweeps: int=1) -> None:
        """
        Render all LIDAR and camera sample_data in sample along with annotations.
        :param token: Sample token.
        :param box_vis_level: If sample_data is an image, this sets required visibility for boxes.
        :param nsweeps: Number of sweeps for lidar and radar.
        """
        record = self.nusc.get('sample', token)

        # Separate RADAR from LIDAR and vision.
        radar_data = {}
        nonradar_data = {}
        for channel, token in record['data'].items():
            sd_record = self.nusc.get('sample_data', token)
            sensor_modality = sd_record['sensor_modality']
            if sensor_modality in ['lidar', 'camera']:
                nonradar_data[channel] = token
            else:
                radar_data[channel] = token

        # Create plots.
        n = 1 + len(nonradar_data)
        cols = 2
        fig, axes = plt.subplots(int(np.ceil(n/cols)), cols, figsize=(16, 24))

        # Plot radar into a single subplot.
        ax = axes[0, 0]
        for i, (_, sd_token) in enumerate(radar_data.items()):
            self.render_sample_data(sd_token, with_anns=i == 0, box_vis_level=box_vis_level, ax=ax, nsweeps=nsweeps)
        ax.set_title('Fused RADARs')

        # Plot camera and lidar in separate subplots.
        for (_, sd_token), ax in zip(nonradar_data.items(), axes.flatten()[1:]):
            self.render_sample_data(sd_token, box_vis_level=box_vis_level, ax=ax, nsweeps=nsweeps)

        axes.flatten()[-1].axis('off')
        plt.tight_layout()
        fig.subplots_adjust(wspace=0, hspace=0)

    def render_sample_data(self, sample_data_token: str, with_anns: bool=True,
                           box_vis_level: BoxVisibility=BoxVisibility.ANY, axes_limit: float=40, ax: Axes=None,
                           nsweeps: int=1) -> None:
        """
        Render sample data onto axis.
        :param sample_data_token: Sample_data token.
        :param with_anns: Whether to draw annotations.
        :param box_vis_level: If sample_data is an image, this sets required visibility for boxes.
        :param axes_limit: Axes limit for lidar and radar (measured in meters).
        :param ax: Axes onto which to render.
        :param nsweeps: Number of sweeps for lidar and radar.
        """

        # Get sensor modality.
        sd_record = self.nusc.get('sample_data', sample_data_token)
        sensor_modality = sd_record['sensor_modality']

        if sensor_modality == 'lidar':
            # Get boxes in lidar frame.
            _, boxes, _ = self.nusc.get_sample_data(sample_data_token, box_vis_level=box_vis_level)

            # Get aggregated point cloud in lidar frame.
            sample_rec = self.nusc.get('sample', sd_record['sample_token'])
            chan = sd_record['channel']
            ref_chan = 'LIDAR_TOP'
            pc, times = LidarPointCloud.from_file_multisweep(self.nusc, sample_rec, chan, ref_chan, nsweeps=nsweeps)

            # Init axes.
            if ax is None:
                _, ax = plt.subplots(1, 1, figsize=(9, 9))

            # Show point cloud.
            points = view_points(pc.points[:3, :], np.eye(4), normalize=False)
            dists = np.sqrt(np.sum(pc.points[:2, :] ** 2, axis=0))
            colors = np.minimum(1, dists/axes_limit/np.sqrt(2))
            ax.scatter(points[0, :], points[1, :], c=colors, s=0.2)

            # Show ego vehicle.
            ax.plot(0, 0, 'x', color='black')

            # Show boxes.
            if with_anns:
                for box in boxes:
                    c = np.array(self.get_color(box.name)) / 255.0
                    box.render(ax, view=np.eye(4), colors=(c, c, c))

            # Limit visible range.
            ax.set_xlim(-axes_limit, axes_limit)
            ax.set_ylim(-axes_limit, axes_limit)

        elif sensor_modality == 'radar':
            # Get boxes in lidar frame.
            sample_rec = self.nusc.get('sample', sd_record['sample_token'])
            lidar_token = sample_rec['data']['LIDAR_TOP']
            _, boxes, _ = self.nusc.get_sample_data(lidar_token, box_vis_level=box_vis_level)

            # Get aggregated point cloud in lidar frame.
            # The point cloud is transformed to the lidar frame for visualization purposes.
            chan = sd_record['channel']
            ref_chan = 'LIDAR_TOP'
            pc, times = RadarPointCloud.from_file_multisweep(self.nusc, sample_rec, chan, ref_chan, nsweeps=nsweeps)

            # Transform radar velocities (x is front, y is left), as these are not transformed when loading the point
            # cloud.
            radar_cs_record = self.nusc.get('calibrated_sensor', sd_record['calibrated_sensor_token'])
            lidar_sd_record = self.nusc.get('sample_data', lidar_token)
            lidar_cs_record = self.nusc.get('calibrated_sensor', lidar_sd_record['calibrated_sensor_token'])
            velocities = pc.points[8:10, :]  # Compensated velocity
            velocities = np.vstack((velocities, np.zeros(pc.points.shape[1])))
            velocities = np.dot(Quaternion(radar_cs_record['rotation']).rotation_matrix, velocities)
            velocities = np.dot(Quaternion(lidar_cs_record['rotation']).rotation_matrix.T, velocities)
            velocities[2, :] = np.zeros(pc.points.shape[1])

            # Init axes.
            if ax is None:
                _, ax = plt.subplots(1, 1, figsize=(9, 9))
                
            # Show point cloud.
            points = view_points(pc.points[:3, :], np.eye(4), normalize=False)
            dists = np.sqrt(np.sum(pc.points[:2, :] ** 2, axis=0))
            colors = np.minimum(1, dists / axes_limit / np.sqrt(2))
            sc = ax.scatter(points[0, :], points[1, :], c=colors, s=3)

            # Show velocities.
            points_vel = view_points(pc.points[:3, :] + velocities, np.eye(4), normalize=False)
            max_delta = 10
            deltas_vel = points_vel - points
            deltas_vel = 3 * deltas_vel  # Arbitrary scaling
            deltas_vel = np.clip(deltas_vel, -max_delta, max_delta)  # Arbitrary clipping
            colors_rgba = sc.to_rgba(colors)
            for i in range(points.shape[1]):
                ax.arrow(points[0, i], points[1, i], deltas_vel[0, i], deltas_vel[1, i], color=colors_rgba[i])

            # Show ego vehicle.
            ax.plot(0, 0, 'x', color='black')

            # Show boxes.
            if with_anns:
                for box in boxes:
                    c = np.array(self.get_color(box.name)) / 255.0
                    box.render(ax, view=np.eye(4), colors=(c, c, c))

            # Limit visible range.
            ax.set_xlim(-axes_limit, axes_limit)
            ax.set_ylim(-axes_limit, axes_limit)
            

        elif sensor_modality == 'camera':
            # Load boxes and image.
            data_path, boxes, camera_intrinsic = self.nusc.get_sample_data(sample_data_token,
                                                                           box_vis_level=box_vis_level)
            data = Image.open(data_path)

            # Init axes.
            if ax is None:
                _, ax = plt.subplots(1, 1, figsize=(9, 16))

            # Show image.
            ax.imshow(data)

            # Show boxes.
            if with_anns:
                for box in boxes:
                    c = np.array(self.get_color(box.name)) / 255.0
                    box.render(ax, view=camera_intrinsic, normalize=True, colors=(c, c, c))

            # Limit visible range.
            ax.set_xlim(0, data.size[0])
            ax.set_ylim(data.size[1], 0)

        else:
            raise ValueError("Unknown sensor modality!")

        ax.axis('off')
        ax.set_title(sd_record['channel'])
        ax.set_aspect('equal')
            
    def render_annotation(self, anntoken: str, margin: float=10, view: np.ndarray=np.eye(4),
                          box_vis_level: BoxVisibility=BoxVisibility.ANY) -> None:
        """
        Render selected annotation.
        :param anntoken: Sample_annotation token.
        :param margin: How many meters in each direction to include in LIDAR view.
        :param view: LIDAR view point.
        :param box_vis_level: If sample_data is an image, this sets required visibility for boxes.
        """

        ann_record = self.nusc.get('sample_annotation', anntoken)
        sample_record = self.nusc.get('sample', ann_record['sample_token'])
        assert 'LIDAR_TOP' in sample_record['data'].keys(), 'No LIDAR_TOP in data, cant render'

        fig, axes = plt.subplots(1, 2, figsize=(18, 9))

        # Figure out which camera the object is fully visible in (this may return nothing)
        boxes, cam = [], []
        cams = [key for key in sample_record['data'].keys() if 'CAM' in key]
        for cam in cams:
            _, boxes, _ = self.nusc.get_sample_data(sample_record['data'][cam], box_vis_level=box_vis_level,
                                                    selected_anntokens=[anntoken])
            if len(boxes) > 0:
                break  # We found an image that matches. Let's abort.
        assert len(boxes) > 0, "Could not find image where annotation if visible. Try using e.g. BoxVisibility.ANY."
        assert len(boxes) < 2, "Found multiple annotations. Something is wrong!"

        cam = sample_record['data'][cam]

        # Plot LIDAR view
        lidar = sample_record['data']['LIDAR_TOP']
        data_path, boxes, camera_intrinsic = self.nusc.get_sample_data(lidar, selected_anntokens=[anntoken])
        LidarPointCloud.from_file(data_path).render_height(axes[0], view=view)
        for box in boxes:
            c = np.array(self.get_color(box.name)) / 255.0
            box.render(axes[0], view=view, colors=(c, c, c))
            corners = view_points(boxes[0].corners(), view, False)[:2, :]
            axes[0].set_xlim([np.min(corners[0, :]) - margin, np.max(corners[0, :]) + margin])
            axes[0].set_ylim([np.min(corners[1, :]) - margin, np.max(corners[1, :]) + margin])
            axes[0].axis('off')
            axes[0].set_aspect('equal')

        # Plot CAMERA view
        data_path, boxes, camera_intrinsic = self.nusc.get_sample_data(cam, selected_anntokens=[anntoken])
        im = Image.open(data_path)
        axes[1].imshow(im)
        axes[1].set_title(self.nusc.get('sample_data', cam)['channel'])
        axes[1].axis('off')
        axes[1].set_aspect('equal')
        for box in boxes:
            c = np.array(self.get_color(box.name)) / 255.0
            box.render(axes[1], view=camera_intrinsic, normalize=True, colors=(c, c, c))

    def render_instance(self, instance_token: str) -> None:
        """
        Finds the annotation of the given instance that is closest to the vehicle, and then renders it.
        :param instance_token: The instance token.
        """
        ann_tokens = self.nusc.field2token('sample_annotation', 'instance_token', instance_token)
        closest = [np.inf, None]
        for ann_token in ann_tokens:
            ann_record = self.nusc.get('sample_annotation', ann_token)
            sample_record = self.nusc.get('sample', ann_record['sample_token'])
            sample_data_record = self.nusc.get('sample_data', sample_record['data']['LIDAR_TOP'])
            pose_record = self.nusc.get('ego_pose', sample_data_record['ego_pose_token'])
            dist = np.linalg.norm(np.array(pose_record['translation']) - np.array(ann_record['translation']))
            if dist < closest[0]:
                closest[0] = dist
                closest[1] = ann_token
        self.render_annotation(closest[1])

    def render_scene(self, scene_token: str, freq: float=10, imsize: Tuple[float, float]=(640, 360),
                     out_path: str=None) -> None:
        """
        Renders a full scene with all camera channels.
        :param scene_token: Unique identifier of scene to render.
        :param freq: Display frequency (Hz).
        :param imsize: Size of image to render. The larger the slower this will run.
        :param out_path: Optional path to write a video file of the rendered frames.
        """

        assert imsize[0] / imsize[1] == 16 / 9, "Aspect ratio should be 16/9."

        # Get records from DB.
        scene_rec = self.nusc.get('scene', scene_token)
        first_sample_rec = self.nusc.get('sample', scene_rec['first_sample_token'])
        last_sample_rec = self.nusc.get('sample', scene_rec['last_sample_token'])

        # Set some display parameters
        layout = {
            'CAM_FRONT_LEFT': (0, 0),
            'CAM_FRONT': (imsize[0], 0),
            'CAM_FRONT_RIGHT': (2 * imsize[0], 0),
            'CAM_BACK_LEFT': (0, imsize[1]),
            'CAM_BACK': (imsize[0], imsize[1]),
            'CAM_BACK_RIGHT': (2 * imsize[0], imsize[1]),
        }

        horizontal_flip = ['CAM_BACK_LEFT', 'CAM_BACK', 'CAM_BACK_RIGHT']  # Flip these for aesthetic reasons.

        time_step = 1 / freq * 1e6  # Time-stamps are measured in micro-seconds.

        window_name = '{}'.format(scene_rec['name'])
        cv2.namedWindow(window_name)
        cv2.moveWindow(window_name, 0, 0)

        canvas = np.ones((2 * imsize[1], 3 * imsize[0], 3), np.uint8)
        if out_path is not None:
            fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            out = cv2.VideoWriter(out_path, fourcc, freq, canvas.shape[1::-1])
        else:
            out = None

        # Load first sample_data record for each channel
        current_recs = {}  # Holds the current record to be displayed by channel.
        prev_recs = {}  # Hold the previous displayed record by channel.
        for channel in layout:
            current_recs[channel] = self.nusc.get('sample_data', first_sample_rec['data'][channel])
            prev_recs[channel] = None

        current_time = first_sample_rec['timestamp']

        while current_time < last_sample_rec['timestamp']:

            current_time += time_step

            # For each channel, find first sample that has time > current_time.
            for channel, sd_rec in current_recs.items():
                while sd_rec['timestamp'] < current_time and sd_rec['next'] != '':
                    sd_rec = self.nusc.get('sample_data', sd_rec['next'])
                    current_recs[channel] = sd_rec

            # Now add to canvas
            for channel, sd_rec in current_recs.items():

                # Only update canvas if we have not already rendered this one.
                if not sd_rec == prev_recs[channel]:

                    # Get annotations and params from DB.
                    impath, boxes, camera_intrinsic = self.nusc.get_sample_data(sd_rec['token'],
                                                                                box_vis_level=BoxVisibility.ANY)

                    # Load and render
                    if not osp.exists(impath):
                        raise Exception('Error: Missing image %s' % impath)
                    im = cv2.imread(impath)
                    for box in boxes:
                        c = self.get_color(box.name)
                        box.render_cv2(im, view=camera_intrinsic, normalize=True, colors=(c, c, c))

                    im = cv2.resize(im, imsize)
                    if channel in horizontal_flip:
                        im = im[:, ::-1, :]

                    canvas[layout[channel][1]: layout[channel][1] + imsize[1],
                           layout[channel][0]:layout[channel][0] + imsize[0], :] = im

                    prev_recs[channel] = sd_rec  # Store here so we don't render the same image twice.

            # Show updated canvas.
            cv2.imshow(window_name, canvas)
            if out_path is not None:
                out.write(canvas)

            key = cv2.waitKey(1)  # Wait a very short time (1 ms).

            if key == 32:  # if space is pressed, pause.
                key = cv2.waitKey()

            if key == 27:  # if ESC is pressed, exit.
                cv2.destroyAllWindows()
                break

        cv2.destroyAllWindows()
        if out_path is not None:
            out.release()

    def render_scene_channel(self, scene_token: str, channel: str='CAM_FRONT', imsize: Tuple[float, float]=(640, 360)):
        """
        Renders a full scene for a particular camera channel.
        :param scene_token: Unique identifier of scene to render.
        :param channel: Channel to render.
        :param imsize: Size of image to render. The larger the slower this will run.
        :return:
        """

        valid_channels = ['CAM_FRONT_LEFT', 'CAM_FRONT', 'CAM_FRONT_RIGHT',
                          'CAM_BACK_LEFT', 'CAM_BACK', 'CAM_BACK_RIGHT']

        assert imsize[0] / imsize[1] == 16 / 9, "Aspect ratio should be 16/9."
        assert channel in valid_channels, 'Input channel {} not valid.'.format(channel)

        # Get records from DB
        scene_rec = self.nusc.get('scene', scene_token)
        sample_rec = self.nusc.get('sample', scene_rec['first_sample_token'])
        sd_rec = self.nusc.get('sample_data', sample_rec['data'][channel])

        # Open CV init
        name = '{}: {} (Space to pause, ESC to exit)'.format(scene_rec['name'], channel)
        cv2.namedWindow(name)
        cv2.moveWindow(name, 0, 0)

        has_more_frames = True
        while has_more_frames:

            # Get data from DB
            impath, boxes, camera_intrinsic = self.nusc.get_sample_data(sd_rec['token'],
                                                                        box_vis_level=BoxVisibility.ANY)

            # Load and render
            if not osp.exists(impath):
                raise Exception('Error: Missing image %s' % impath)
            im = cv2.imread(impath)
            for box in boxes:
                c = self.get_color(box.name)
                box.render_cv2(im, view=camera_intrinsic, normalize=True, colors=(c, c, c))

            # Render
            im = cv2.resize(im, imsize)
            cv2.imshow(name, im)

            key = cv2.waitKey(10)  # Images stored at approx 10 Hz, so wait 10 ms.
            if key == 32:  # If space is pressed, pause.
                key = cv2.waitKey()

            if key == 27:  # if ESC is pressed, exit
                cv2.destroyAllWindows()
                break

            if not sd_rec['next'] == "":
                sd_rec = self.nusc.get('sample_data', sd_rec['next'])
            else:
                has_more_frames = False

        cv2.destroyAllWindows()

    def render_egoposes_on_map(self, log_location: str, scene_tokens: List=None, demo_ss_factor: float=2.0) \
            -> None:
        """
        Renders ego poses a the map. These can be filtered by location or scene.
        :param log_location: Name of the location, e.g. "singapore-onenorth", "boston-seaport".
        :param scene_tokens: Optional list of scene tokens.
        :param demo_ss_factor: Subsampling factor for rendering the map.
        """

        # Settings
        close_dist = 100
        pixel_to_meter = 0.1
        ignore_logfiles = ['n008-2018-05-21-11-06-59-0400']  # Exclude older logs with incompatible maps

        # Get logs by location
        log_tokens = [l['token'] for l in self.nusc.log if l['location'] == log_location
                      and l['logfile'] not in ignore_logfiles]
        assert len(log_tokens) > 0

        # Filter scenes
        scene_tokens_location = [e['token'] for e in self.nusc.scene if e['log_token'] in log_tokens]
        if scene_tokens is not None:
            scene_tokens_location = [t for t in scene_tokens_location if t in scene_tokens]
        if len(scene_tokens_location) == 0:
            print('Warning: Found 0 valid scenes for location %s!' % log_location)

        map_poses = []
        map_mask = None

        for scene_token in tqdm(scene_tokens_location):

            # Get records from the database.
            scene_record = self.nusc.get('scene', scene_token)
            log_record = self.nusc.get('log', scene_record['log_token'])
            map_record = self.nusc.get('map', log_record['map_token'])
            map_mask = map_record['mask']

            # For each sample in the scene, store the ego pose.
            sample_tokens = self.nusc.field2token('sample', 'scene_token', scene_token)
            for sample_token in sample_tokens:
                sample_record = self.nusc.get('sample', sample_token)

                # Poses are associated with the sample_data. Here we use the lidar sample_data.
                sample_data_record = self.nusc.get('sample_data', sample_record['data']['LIDAR_TOP'])
                pose_record = self.nusc.get('ego_pose', sample_data_record['ego_pose_token'])

                # Recover the ego pose. A 1 is added at the end to make it homogenous coordinates.
                pose = np.array(pose_record['translation'] + [1])

                # Calculate the pose on the map.
                map_pose = np.dot(map_mask.transform_matrix, pose)
                map_pose = map_pose[:2]
                map_poses.append(map_pose)

        # Compute number of close ego poses.
        map_poses = np.vstack(map_poses)
        dists = sklearn.metrics.pairwise.euclidean_distances(map_poses * pixel_to_meter)
        close_poses = np.sum(dists < close_dist, axis=0)

        # Plot.
        _, ax = plt.subplots(1, 1, figsize=(10, 10))
        mask = Image.fromarray(map_mask.mask)
        size_x = int(mask.size[0] / demo_ss_factor)
        size_y = int(mask.size[1] / demo_ss_factor)
        ax.imshow(mask.resize((size_x, size_y), resample=Image.NEAREST))
        title = 'Number of ego poses within {}m in {}'.format(close_dist, log_location)
        ax.set_title(title, color='w')
        sc = ax.scatter(map_poses[:, 0] / demo_ss_factor, map_poses[:, 1] / demo_ss_factor, s=10, c=close_poses)
        color_bar = plt.colorbar(sc, fraction=0.025, pad=0.04)
        plt.rcParams['figure.facecolor'] = 'black'
        color_bar_ticklabels = plt.getp(color_bar.ax.axes, 'yticklabels')
        plt.setp(color_bar_ticklabels, color='w')
        plt.rcParams['figure.facecolor'] = 'white'  # Reset for future plots

    def save_sample_token(self, start_sample_token: str) -> List[str]:
        """
        Save sample token from one start token.
        :param start_token: The start sample token.
        :return: <list>. All sample token.
        """
        
        tmp_sample = self.nusc.get('sample', start_sample_token)
        sample_list = []
        
        has_more_frames = True
        while has_more_frames:

            sample_list.append(tmp_sample['token'])

            if not tmp_sample['next'] == "":
                tmp_sample = self.nusc.get('sample',tmp_sample['next'])
            else:
                has_more_frames = False
                
        return sample_list

    def save_sample_data(self, start_sample_token: str, channel: str='RADAR_FRONT', with_anns: bool=True,
                           box_vis_level: BoxVisibility=BoxVisibility.ANY, axes_limit: float=40, ax: Axes=None,
                           nsweeps: int=1) -> None :
        """
        Render and save sample data into picture.
        :param sample_token_list: List of all sample token.
        :param with_anns: Whether to draw annotations.
        :param box_vis_level: If sample_data is an image, this sets required visibility for boxes.
        :param axes_limit: Axes limit for lidar and radar (measured in meters).
        :param ax: Axes onto which to render.
        :param nsweeps: Number of sweeps for lidar and radar.
        :return <list>. Information about pictures: scene name, sample number number and channel.
        """
        
        # get the list of sample token
        sample_token_list = self.save_sample_token(start_sample_token)
        print('Sample number is ',len(sample_token_list),'.')
        
        pic_id = 0
        for sample_id in sample_token_list:

            # Get sensor modality.
            sample_recode = self.nusc.get('sample',sample_id)
            
            # Channel existence judgement
            if channel not in sample_recode['data']:
                print('Warning: No such channel !')
                print('You can choose the following channel:')
                print(list(sample_recode['data']))
                return None
            
            sd_record = self.nusc.get('sample_data', sample_recode['data'][channel])
            sensor_modality = sd_record['sensor_modality']

            if sensor_modality == 'lidar':
                # Get boxes in lidar frame.
                sample_data_token = sd_record['token']
                _, boxes, _ = self.nusc.get_sample_data(sample_data_token, box_vis_level=box_vis_level)

                # Get aggregated point cloud in lidar frame.
                sample_rec = self.nusc.get('sample', sd_record['sample_token'])
                chan = sd_record['channel']
                ref_chan = 'LIDAR_TOP'
                pc, times = LidarPointCloud.from_file_multisweep(self.nusc, sample_rec, chan, ref_chan, nsweeps=nsweeps)

                # Init axes.
                if ax is None:
                    fig, ax = plt.subplots(1, 1, figsize=(9, 9))

                # Show point cloud.
                points = view_points(pc.points[:3, :], np.eye(4), normalize=False)
                dists = np.sqrt(np.sum(pc.points[:2, :] ** 2, axis=0))
                colors = np.minimum(1, dists/axes_limit/np.sqrt(2))
                ax.scatter(points[0, :], points[1, :], c=colors, s=0.2)

                # Show ego vehicle.
                ax.plot(0, 0, 'x', color='black')

                # Show boxes.
                if with_anns:
                    for box in boxes:
                        c = np.array(self.get_color(box.name)) / 255.0
                        box.render(ax, view=np.eye(4), colors=(c, c, c))

                # Limit visible range.
                ax.set_xlim(-axes_limit, axes_limit)
                ax.set_ylim(-axes_limit, axes_limit)

            elif sensor_modality == 'radar':
                # Get boxes in lidar frame.
                sample_rec = self.nusc.get('sample', sd_record['sample_token'])
                lidar_token = sample_rec['data']['LIDAR_TOP']
                _, boxes, _ = self.nusc.get_sample_data(lidar_token, box_vis_level=box_vis_level)

                # Get aggregated point cloud in lidar frame.
                # The point cloud is transformed to the lidar frame for visualization purposes.
                chan = sd_record['channel']
                ref_chan = 'LIDAR_TOP'
                pc, times = RadarPointCloud.from_file_multisweep(self.nusc, sample_rec, chan, ref_chan, nsweeps=nsweeps)

                # Transform radar velocities (x is front, y is left), as these are not transformed when loading the point
                # cloud.
                radar_cs_record = self.nusc.get('calibrated_sensor', sd_record['calibrated_sensor_token'])
                lidar_sd_record = self.nusc.get('sample_data', lidar_token)
                lidar_cs_record = self.nusc.get('calibrated_sensor', lidar_sd_record['calibrated_sensor_token'])
                velocities = pc.points[8:10, :]  # Compensated velocity
                velocities = np.vstack((velocities, np.zeros(pc.points.shape[1])))
                velocities = np.dot(Quaternion(radar_cs_record['rotation']).rotation_matrix, velocities)
                velocities = np.dot(Quaternion(lidar_cs_record['rotation']).rotation_matrix.T, velocities)
                velocities[2, :] = np.zeros(pc.points.shape[1])

                # Init axes.
                if ax is None:
                    fig, ax = plt.subplots(1, 1, figsize=(9, 9))

                # Show point cloud.
                points = view_points(pc.points[:3, :], np.eye(4), normalize=False)
                dists = np.sqrt(np.sum(pc.points[:2, :] ** 2, axis=0))
                colors = np.minimum(1, dists / axes_limit / np.sqrt(2))
                sc = ax.scatter(points[0, :], points[1, :], c=colors, s=3)

                # Show velocities.
                points_vel = view_points(pc.points[:3, :] + velocities, np.eye(4), normalize=False)
                max_delta = 10
                deltas_vel = points_vel - points
                deltas_vel = 3 * deltas_vel  # Arbitrary scaling
                deltas_vel = np.clip(deltas_vel, -max_delta, max_delta)  # Arbitrary clipping
                colors_rgba = sc.to_rgba(colors)
                for i in range(points.shape[1]):
                    ax.arrow(points[0, i], points[1, i], deltas_vel[0, i], deltas_vel[1, i], color=colors_rgba[i])

                # Show ego vehicle.
                ax.plot(0, 0, 'x', color='black')

                # Show boxes.
                if with_anns:
                    for box in boxes:
                        c = np.array(self.get_color(box.name)) / 255.0
                        box.render(ax, view=np.eye(4), colors=(c, c, c))

                # Limit visible range.
                ax.set_xlim(-axes_limit, axes_limit)
                ax.set_ylim(-axes_limit, axes_limit)


            elif sensor_modality == 'camera':
                # Load boxes and image.
                data_path, boxes, camera_intrinsic = self.nusc.get_sample_data(sample_data_token,
                                                                               box_vis_level=box_vis_level)
                data = Image.open(data_path)

                # Init axes.
                if ax is None:
                    fig, ax = plt.subplots(1, 1, figsize=(9, 16))

                # Show image.
                ax.imshow(data)

                # Show boxes.
                if with_anns:
                    for box in boxes:
                        c = np.array(self.get_color(box.name)) / 255.0
                        box.render(ax, view=camera_intrinsic, normalize=True, colors=(c, c, c))

                # Limit visible range.
                ax.set_xlim(0, data.size[0])
                ax.set_ylim(data.size[1], 0)

            else:
                raise ValueError("Unknown sensor modality!")

            ax.axis('off')
            ax.set_title(sd_record['channel'])
            ax.set_aspect('equal')

            pic_name = './pic_tmp/'+str(pic_id)+'.png'
            plt.savefig(pic_name)
            pic_id += 1
            plt.axis('off')
            plt.cla()
            
        # Log information about the picture
        scene_rec = self.nusc.get('scene', sample_rec['scene_token'])
        inf_list = [scene_rec['name'], len(sample_token_list), channel]
        with open("./pic_tmp/inf_list.txt","w") as f:
            for i in inf_list:
                f.write(str(i))
                f.write(',')

    
    def render_pointcloud_channel(self) -> None:
        """
        Renders a full scene for a particular camera channel.
        """
        
        # Get the information about pictures: scene name, sample number number and channel.
        inf_list = []
        with open("./pic_tmp/inf_list.txt","r") as f:
            for line in f:
                inf_list = line.strip('\n').split(',')
        scene_name = inf_list [0]
        sample_num = inf_list [1]
        channel = inf_list [2]
        
        # Open CV init
        name = '{}: {} (Space to pause, ESC to exit)'.format(scene_name, channel)
        cv2.namedWindow(name)
        cv2.moveWindow(name, 0, 0)

        for pic_id in range(int(sample_num)):

            impath = './pic_tmp/'+str(pic_id)+'.png'
            # Load and render
            if not osp.exists(impath):
                raise Exception('Error: Missing image %s' % impath)
            im = cv2.imread(impath)

            # Render
            cv2.imshow(name, im)

            key = cv2.waitKey(100)  # Images stored at approx 10 Hz, so wait 10 ms.
            if key == 32:  # If space is pressed, pause.
                key = cv2.waitKey()

            if key == 27:  # if ESC is pressed, exit
                cv2.destroyAllWindows()
                break


        cv2.destroyAllWindows()
 

    def get_pointcloud_position(self, pointsensor_token: str) -> Tuple:
        """
        Given a point sensor (lidar/radar) token and camera sample_data token, load point-cloud and map it to the image
        plane.
        :param pointsensor_token: Lidar/radar sample_data token.
        :param camera_token: Camera sample_data token.
        :return (pointcloud <np.float: 2, n)>).
        """

        pointsensor = self.nusc.get('sample_data', pointsensor_token)
        pcl_path, _, _ = self.nusc.get_sample_data(pointsensor_token)

        if pointsensor['sensor_modality'] == 'lidar':
            pc = LidarPointCloud.from_file(pcl_path)
        else:
            pc = RadarPointCloud.from_file(pcl_path)
        
        # Get the right view
        view = np.eye(4)   
        points = view_points(pc.points[:3, :], view, normalize=False)
        
        # Plot the pointcloud
        fig, ax = plt.subplots(1, 1, figsize=(9, 9))
        ax.scatter(points[0, :], points[1, :], c=points[2, :], s=1)
        ax.set_xlim(-40, 40)
        ax.set_ylim(-40, 40)
        ax.axis('off')
        ax.set_aspect('equal')
        
        return points
    
    def plot_pointcloud_prediction(self, obj_mean) -> None:
        """
        Given a point sensor (lidar/radar) token and camera sample_data token, load point-cloud and map it to the image
        plane.
        :param pointsensor_token: Lidar/radar sample_data token.
        :param camera_token: Camera sample_data token.
        :return (pointcloud <np.float: 2, n)>).
        """

#         pointsensor = self.nusc.get('sample_data', pointsensor_token)
#         pcl_path, _, _ = self.nusc.get_sample_data(pointsensor_token)

#         if pointsensor['sensor_modality'] == 'lidar':
#             pc = LidarPointCloud.from_file(pcl_path)
#         else:
#             pc = RadarPointCloud.from_file(pcl_path)
        
#         # Get the right view
#         view = np.eye(4)   
#         points = view_points(pc.points[:3, :], view, normalize=False)
        
#         # Plot the pointcloud
#         fig, ax = plt.subplots(1, 1, figsize=(9, 9))
#         ax.scatter(points[0, :], points[1, :], c=points[2, :], s=1)
#         ax.set_xlim(-40, 40)
#         ax.set_ylim(-40, 40)
#         ax.axis('off')
#         ax.set_aspect('equal')
        
        return points