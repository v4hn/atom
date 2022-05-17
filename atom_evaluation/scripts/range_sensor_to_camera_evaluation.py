#!/usr/bin/env python3

"""
Reads the calibration results from a json file and computes the evaluation metrics
"""

# -------------------------------------------------------------------------------
# --- IMPORTS
# -------------------------------------------------------------------------------

# Standard imports
import json
import math
import os
import argparse
import sys
from collections import OrderedDict

import numpy as np
import ros_numpy
import cv2
from prettytable import PrettyTable
from scipy.spatial import distance
from colorama import Style, Fore

# ROS imports
from rospy_message_converter import message_converter

# Atom imports
from atom_core.naming import generateKey
from atom_core.vision import projectToCamera
from atom_core.dataset_io import getPointCloudMessageFromDictionary, read_pcd
from atom_core.atom import getTransform

# -------------------------------------------------------------------------------
# --- FUNCTIONS
# -------------------------------------------------------------------------------


def rangeToImage(collection, json_file, ss, ts, tf):
    filename = os.path.dirname(json_file) + '/' + collection['data'][ss]['data_file']
    msg = read_pcd(filename)
    collection['data'][ss].update(message_converter.convert_ros_message_to_dictionary(msg))

    cloud_msg = getPointCloudMessageFromDictionary(collection['data'][ss])
    idxs = collection['labels'][ss]['idxs_limit_points']

    pc = ros_numpy.numpify(cloud_msg)[idxs]
    points_in_vel = np.zeros((4, pc.shape[0]))
    points_in_vel[0, :] = pc['x']
    points_in_vel[1, :] = pc['y']
    points_in_vel[2, :] = pc['z']
    points_in_vel[3, :] = 1

    points_in_cam = np.dot(tf, points_in_vel)

    # -- Project them to the image
    w, h = collection['data'][ts]['width'], collection['data'][ts]['height']
    K = np.ndarray((3, 3), buffer=np.array(test_dataset['sensors'][ts]['camera_info']['K']), dtype=float)
    D = np.ndarray((5, 1), buffer=np.array(test_dataset['sensors'][ts]['camera_info']['D']), dtype=float)

    pts_in_image, _, _ = projectToCamera(K, D, w, h, points_in_cam[0:3, :])

    return pts_in_image

# -------------------------------------------------------------------------------
# --- MAIN
# -------------------------------------------------------------------------------


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-train_json", "--train_json_file", help="Json file containing input training dataset.", type=str,
                    required=True)
    ap.add_argument("-test_json", "--test_json_file", help="Json file containing input testing dataset.", type=str,
                    required=True)
    ap.add_argument("-rs", "--range_sensor", help="Source transformation sensor.", type=str, required=True)
    ap.add_argument("-cs", "--camera_sensor", help="Target transformation sensor.", type=str, required=True)
    ap.add_argument("-si", "--show_images", help="If true the script shows images.", action='store_true', default=False)
    # ap.add_argument("-ef", "--eval_file", help="Path to file to read and/or write the evalutation data.", type=str,
    #                 required=True)
    # ap.add_argument("-ua", "--use_annotation", help="If true, the limit points will be manually annotated.",
    #                 action='store_true', default=False)

    # - Save args
    args = vars(ap.parse_args())
    range_sensor = args['range_sensor']
    camera_sensor = args['camera_sensor']
    show_images = args['show_images']
    # eval_file = args['eval_file']
    # use_annotation = args['use_annotation']

    # ---------------------------------------
    # --- INITIALIZATION Read calibration data from file
    # ---------------------------------------
    # Loads a json file containing the calibration
    train_json_file = args['train_json_file']
    f = open(train_json_file, 'r')
    train_dataset = json.load(f)
    test_json_file = args['test_json_file']
    f = open(test_json_file, 'r')
    test_dataset = json.load(f)

    annotation_file = os.path.dirname(test_json_file) + "/annotation_" + camera_sensor + ".json"
    if os.path.exists(annotation_file) is False:
        raise ValueError(
            'Annotation file does not exist. Please annotate using the command rosrun atom_evaluation annotate.py -test_json {path_to_folder} -cs {souce_sensor} -si)')
        exit(0)
    # ---------------------------------------
    # --- Get mixed json (calibrated transforms from train and the rest from test)
    # ---------------------------------------
    # test_dataset = test_dataset
    # I am just using the test dataset for everything. If we need an original test dataset we can copy here.

    # Replace optimized transformations in the test dataset copying from the train dataset
    for sensor_key, sensor in train_dataset['sensors'].items():
        calibration_parent = sensor['calibration_parent']
        calibration_child = sensor['calibration_child']
        transform_name = generateKey(calibration_parent, calibration_child)

        # We can only optimized fixed transformations, so the optimized transform should be the same for all
        # collections. We select the first collection (selected_collection_key) and retrieve the optimized
        # transformation for that.
        selected_collection_key = list(train_dataset['collections'].keys())[0]
        optimized_transform = train_dataset['collections'][selected_collection_key]['transforms'][transform_name]

        # iterate all collections of the test dataset and replace the optimized transformation
        for collection_key, collection in test_dataset['collections'].items():
            collection['transforms'][transform_name]['quat'] = optimized_transform['quat']
            collection['transforms'][transform_name]['trans'] = optimized_transform['trans']

    # Copy intrinsic parameters for cameras from train to test dataset.
    for train_sensor_key, train_sensor in train_dataset['sensors'].items():
        if train_sensor['msg_type'] == 'Image':
            test_dataset['sensors'][train_sensor_key]['camera_info']['D'] = train_sensor['camera_info']['D']
            test_dataset['sensors'][train_sensor_key]['camera_info']['K'] = train_sensor['camera_info']['K']
            test_dataset['sensors'][train_sensor_key]['camera_info']['P'] = train_sensor['camera_info']['P']
            test_dataset['sensors'][train_sensor_key]['camera_info']['R'] = train_sensor['camera_info']['R']

    # ---------------------------------------
    # --- INITIALIZATION Read evaluation data from file ---> if desired <---
    # ---------------------------------------
    f = open(annotation_file, 'r')
    annotations = json.load(f)

    # Deleting collections where the pattern is not found by all sensors:
    collections_to_delete = []
    for collection_key, collection in test_dataset['collections'].items():
        for sensor_key, sensor in test_dataset['sensors'].items():
            if not collection['labels'][sensor_key]['detected'] and (
                    sensor_key == camera_sensor or sensor_key == range_sensor):
                print(
                    Fore.RED + "Removing collection " + collection_key + ' -> pattern was not found in sensor ' +
                    sensor_key + ' (must be found in all sensors).' + Style.RESET_ALL)

                collections_to_delete.append(collection_key)
                break

    for collection_key in collections_to_delete:
        del test_dataset['collections'][collection_key]

    print(Fore.BLUE + '\nStarting evalutation...' + Style.RESET_ALL)

    from_frame = test_dataset['calibration_config']['sensors'][camera_sensor]['link']
    to_frame = test_dataset['calibration_config']['sensors'][range_sensor]['link']
    od = OrderedDict(sorted(test_dataset['collections'].items(), key=lambda t: int(t[0])))
    e = {}  # dictionary with all the errors
    for collection_key, collection in od.items():
        e[collection_key] = {}  # init the dictionary of errors for this collection
        # ---------------------------------------
        # --- Range to image projection
        # ---------------------------------------
        vel2cam = getTransform(from_frame, to_frame,
                               test_dataset['collections'][collection_key]['transforms'])
        pts_in_image = rangeToImage(collection, test_json_file, range_sensor, camera_sensor, vel2cam)

        # ---------------------------------------
        # --- Get evaluation data for current collection
        # ---------------------------------------
        filename = os.path.dirname(test_json_file) + '/' + collection['data'][camera_sensor]['data_file']
        image = cv2.imread(filename)

        if args['show_images']:  # draw all ground truth annotations
            for side in annotations[collection_key].keys():
                for x, y in zip(annotations[collection_key][side]['ixs'],
                                annotations[collection_key][side]['iys']):
                    cv2.circle(image, (int(round(x)), int(round(y))), 1, (0, 255, 0), -1)

        # ---------------------------------------
        # --- Evaluation metrics - reprojection error
        # ---------------------------------------
        # -- For each reprojected limit point, find the closest ground truth point and compute the distance to it
        x_errors = []
        y_errors = []
        rms_errors = []
        for idx in range(0, pts_in_image.shape[1]):
            x_proj = pts_in_image[0, idx]
            y_proj = pts_in_image[1, idx]

            # Do not consider points that are re-projected outside of the image
            if x_proj > image.shape[1] or x_proj < 0 or y_proj > image.shape[0] or y_proj < 0:
                continue

            min_error = sys.float_info.max  # a very large value
            x_min = None
            y_min = None
            for side in annotations[collection_key].keys():
                for x, y in zip(annotations[collection_key][side]['ixs'],
                                annotations[collection_key][side]['iys']):
                    error = math.sqrt((x_proj-x)**2 + (y_proj-y)**2)
                    if error < min_error:
                        min_error = error
                        x_min = x
                        y_min = y

            x_errors.append(abs(x_proj - x_min))
            y_errors.append(abs(y_proj - y_min))
            rms_errors.append(min_error)

            if args['show_images']:
                cv2.circle(image, (int(round(x_proj)), int(round(y_proj))), 5, (255, 0, 0), -1)
                cv2.line(image, (int(round(x_proj)), int(round(y_proj))),
                         (int(round(x_min)), int(round(y_min))), (0, 255, 255, 1))

        if not rms_errors:
            print('No LiDAR point mapped into the image for collection ' + str(collection_key))
            continue

        e[collection_key]['x'] = np.average(x_errors)
        e[collection_key]['y'] = np.average(y_errors)
        e[collection_key]['rms'] = np.average(rms_errors)

        if args['show_images']:
            print('Errors collection ' + collection_key + '\n' + str(e[collection_key]))
            window_name = 'Collection ' + collection_key
            cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
            cv2.imshow(window_name, image)
            cv2.waitKey(0)

    # -------------------------------------------------------------
    # Print output table
    # -------------------------------------------------------------
    table_header = ['Collection #', 'RMS (pix)', 'X err (pix)', 'Y err (pix)']
    table = PrettyTable(table_header)

    od = OrderedDict(sorted(test_dataset['collections'].items(), key=lambda t: int(t[0])))
    for collection_key, collection in od.items():
        row = [collection_key,
               '%.4f' % e[collection_key]['rms'],
               '%.4f' % e[collection_key]['x'],
               '%.4f' % e[collection_key]['y']]

        table.add_row(row)

    # Compute averages and add a bottom row
    bottom_row = []  # Compute averages and add bottom row to table
    for col_idx, _ in enumerate(table_header):
        if col_idx == 0:
            bottom_row.append(Fore.BLUE + Style.BRIGHT + 'Averages' + Fore.BLACK + Style.NORMAL)
            continue

        total = 0
        count = 0
        for row in table.rows:
            # if row[col_idx].isnumeric():
            try:
                value = float(row[col_idx])
                total += float(value)
                count += 1
            except:
                pass

        value = '%.4f' % (total / count)
        bottom_row.append(Fore.BLUE + value + Fore.BLACK)

    table.add_row(bottom_row)

    # Put larger errors in red per column (per sensor)
    for col_idx, _ in enumerate(table_header):
        if col_idx == 0:  # nothing to do
            continue

        max = 0
        max_row_idx = 0
        for row_idx, row in enumerate(table.rows[:-1]):  # ignore bottom row
            try:
                value = float(row[col_idx])
            except:
                continue

            if value > max:
                max = value
                max_row_idx = row_idx

        # set the max column value to red
        table.rows[max_row_idx][col_idx] = Fore.RED + table.rows[max_row_idx][col_idx] + Style.RESET_ALL

    table.align = 'c'
    print(Style.BRIGHT + 'Errors per collection' + Style.RESET_ALL)
    print(table)

    print('Ending script...')
    sys.exit()
