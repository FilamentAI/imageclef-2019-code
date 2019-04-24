import warnings
import os
import shutil
import json

import numpy as np
import torch

from tensorboardX import SummaryWriter


def mask_to_one_hot(mask, colour_mapping):
    """
    Turn a mask that contains colour into a one-hot-encoded array
    :param mask:
    :param colour_mapping: dict of type {<class_name>: <colour>}

    :return:
    """

    one_hot = np.zeros(mask.shape[:2] + (len(colour_mapping.keys()),))

    for i, k in enumerate(sorted(colour_mapping.keys())):
        one_hot[mask == colour_mapping[k], i] = 1

    return one_hot


def colour_mask_to_class_id_mask(colour_mask, colour_mapping):
    """
    :param colour_mask:
    :param colour_mapping:
    :return:
    """

    class_id_mask = np.zeros(colour_mask.shape[:2]).astype(np.uint8)

    for i, k in enumerate(sorted(colour_mapping.keys())):
        class_id_mask[colour_mask == colour_mapping[k]] = i

    return class_id_mask


def class_id_mask_to_colour_mask(class_id_mask, colour_mapping):
    """
    :param class_id_mask:
    :param colour_mapping:
    :return:
    """

    colour_mask = np.zeros(class_id_mask.shape[:2]).astype(np.uint8)

    for i, k in enumerate(sorted(colour_mapping.keys())):
        colour_mask[class_id_mask == i] = colour_mapping[k]

    return colour_mask


def one_hot_to_mask(one_hot, colour_mapping):
    """
    Turn a one_hot_encoded array mask that contains colours
    :param one_hot:
    :param colour_mapping: dict of type {<class_name>: <colour>}

    :return:
    """
    mask = np.zeros(one_hot.shape[:2]).astype(np.uint8)

    for i, k in enumerate(sorted(colour_mapping.keys())):
        mask[one_hot[:, :, i] == 1] = colour_mapping[k]

    return mask


def calculate_class_weights(class_stats_file_path, colour_mapping, modifier=1.01):
    with open(class_stats_file_path, "r") as fp:
        stats = json.load(fp)

    shares = [stats[c]["share"] for c in sorted(colour_mapping.keys())]
    shares = np.array(shares)
    class_weights = 1 / np.log(modifier + shares)
    return class_weights


def load_state_dict(model, filepath):
    pretrained_dict = torch.load(filepath, map_location=lambda storage, loc: storage)
    model_dict = model.state_dict()

    # 1. filter out unnecessary keys and mismatching sizes
    pretrained_dict = {k: v for k, v in pretrained_dict.items() if
                       (k in model_dict) and (model_dict[k].shape == pretrained_dict[k].shape)}

    # 2. overwrite entries in the existing state dict
    model_dict.update(pretrained_dict)
    # 3. load the new state dict
    model.load_state_dict(model_dict)


def cut_windows(image, window_size, step_size=None):
    """
    Cut an image into several, equally sized windows. step size determines the overlap of the windows.
    :param image: image to be cut
    :param window_size: size of the square windows
    :param step_size: step size between windows, determines overlap. Depending on the image size, window size and
     step size it may not be possible to ensure the given step size since a constant window size is preferred
    :return: list of cut images and list of original, upper left corner points (x, y)
    """
    step_size = int(window_size / 2) if step_size is None else step_size

    h, w = image.shape[:2]
    cuts = []
    start_points = []

    for x in range(0, w - step_size, step_size):
        end_x = np.min([x + window_size, w])
        start_x = end_x - window_size

        # stop if the current rectangle has been done before
        if len(start_points) > 0 and start_x == start_points[-1][0]:
            break

        for y in range(0, h - step_size, step_size):
            end_y = np.min([y + window_size, h])
            start_y = end_y - window_size

            # stop if the current rectangle has been done before
            if len(start_points) > 0 and start_y == start_points[-1][1]:
                break

            cuts.append(image[start_y:end_y, start_x:end_x])
            start_points.append([start_x, start_y])

            # print("x: {}/ y:{} to x: {}/ y:{}".format(start_x, start_y, end_x, end_y))

    return cuts, start_points


def calc_rect_size(rect):
    """
    Calculated the area of a rectangle
    :param rect: rectangle dict
    :return: area
    """
    return (rect[3] - rect[1]) * (rect[2] - rect[0])


def calc_intersection(rect1, rect2):
    """
    Calculate the intersection area of two rectangles
    :param rect1: rectangle list
    :param rect2: rectangle list
    :return: union area
    """
    x1 = max([rect1[0], rect2[0]])
    y1 = max([rect1[1], rect2[1]])
    x2 = min([rect1[2], rect2[2]])
    y2 = min([rect1[3], rect2[3]])

    if (x2 < x1) or (y2 < y1):
        return 0

    return (x2 - x1) * (y2 - y1)


def calc_union(rect1, rect2):
    """
    Calculate the union area of two rectangles
    :param rect1: rectangle dict
    :param rect2: rectangle dict
    :return: union area
    """
    return calc_rect_size(rect1) + calc_rect_size(rect2) - calc_intersection(rect1, rect2)


def calc_IOU(rect1, rect2):
    """
    Calculate intersection over union of two rectangles. This is a measure of how similar rectangles
    :param rect1: rectangle dict
    :param rect2: rectangle dict
    :return: IOU area
    """
    return calc_intersection(rect1, rect2) / calc_union(rect1, rect2)


class Saver(object):

    def __init__(self, folder_path, instructions):
        self.instructions = instructions
        self.folder_path = folder_path

    def save_checkpoint(self, model, is_best, epoch):
        file_path = os.path.join(self.folder_path, "checkpoint_epoch_{}.pt".format(epoch))
        torch.save(model.state_dict(), file_path)
        if is_best:
            shutil.copyfile(file_path, os.path.join(self.folder_path, 'model_best.pth'))

    def save_instructions(self):
        with open(os.path.join(self.folder_path, "instructions.json"), "w") as fp:
            json.dump(self.instructions, fp, indent=4, sort_keys=True)


class TensorboardSummary(object):
    def __init__(self, directory):
        self.directory = directory

    def create_summary(self):
        writer = SummaryWriter(log_dir=os.path.join(self.directory))
        return writer
