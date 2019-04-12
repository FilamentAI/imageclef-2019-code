import os
import json
from pprint import pprint
import sys

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torch.optim as optim
from torchvision import transforms

import numpy as np
from tqdm import tqdm

from coral_reef.constants import paths
from coral_reef.constants import strings as STR

from coral_reef.ml.data_set import DictArrayDataSet, RandomCrop, Resize, custom_collate, ToTensor, Flip
from coral_reef.ml.utils import load_state_dict, Saver

sys.path.extend([paths.DEEPLAB_FOLDER_PATH])

from modeling.deeplab import DeepLab
from utils.loss import SegmentationLosses
from utils.metrics import Evaluator
from utils.lr_scheduler import LR_Scheduler
from utils.saver import Saver
from utils.summaries import TensorboardSummary


class Trainer:

    def __init__(self, data_train, data_valid, image_base_dir, instructions):
        """

        :param data_train:
        :param data_valid:
        :param image_base_dir:
        :param instructions:
        """

        # specify model save dir
        self.model_name = instructions[STR.MODEL_NAME]
        experiment_folder_path = os.path.join(paths.MODELS_FOLDER_PATH, self.model_name)
        os.makedirs(experiment_folder_path, exist_ok=False)

        # define saver and save instructions
        self.saver = Saver(folder_path=experiment_folder_path,
                           instructions=instructions)
        self.saver.save_instructions()

        # define Tensorboard Summary
        self.summary = TensorboardSummary(experiment_folder_path)
        self.writer = self.summary.create_summary()

        nn_input_size = instructions[STR.NN_INPUT_SIZE]
        state_dict_file_path = instructions.get(STR.STATE_DICT_FILE_PATH, None)

        # load colour mapping
        with open(os.path.join(instructions[STR.COLOUR_MAPPING_FILE_PATH]), "r") as fp:
            colour_mapping = json.load(fp)

        # define transformers for training and validation
        crops_per_image = instructions.get(STR.CROPS_PER_IMAGE, 10)
        random_crop = RandomCrop(min_size=400, max_size=1000, crop_count=crops_per_image)
        transformations = transforms.Compose([random_crop, Resize(nn_input_size), Flip(), ToTensor()])

        # define batch size
        self.batch_size = crops_per_image * instructions.get(STR.IMAGES_PER_BATCH)

        # set up data loaders
        dataset_train = DictArrayDataSet(image_base_dir=image_base_dir,
                                         data=data_train,
                                         colour_mapping=colour_mapping,
                                         transformation=transformations)

        self.data_loader_train = DataLoader(dataset=dataset_train,
                                            batch_size=int(self.batch_size / crops_per_image),
                                            shuffle=True,
                                            collate_fn=custom_collate)

        dataset_valid = DictArrayDataSet(image_base_dir=image_base_dir,
                                         data=data_valid,
                                         colour_mapping=colour_mapping,
                                         transformation=transformations)

        self.data_loader_valid = DataLoader(dataset=dataset_valid,
                                            batch_size=int(self.batch_size / crops_per_image),
                                            shuffle=False,
                                            collate_fn=custom_collate)

        self.num_classes = dataset_train.num_classes()

        # define model
        print("Building model")
        self.model = DeepLab(num_classes=self.num_classes,
                             backbone="resnet")

        # load weights
        if state_dict_file_path is not None:
            print("loading state_dict from:")
            print(state_dict_file_path)
            load_state_dict(self.model, state_dict_file_path)

        # choose gpu or cpu
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        # if torch.cuda.device_count() > 1:
        #   print("Let's use ", torch.cuda.device_count(), " GPUs!")
        #   temp_net = nn.DataParallel(temp_net)

        self.model.to(self.device)

        train_params = [{'params': self.model.get_1x_lr_params(), 'lr': instructions.get(STR.LEARNING_RATE, 1e-5)},
                        {'params': self.model.get_10x_lr_params(), 'lr': instructions.get(STR.LEARNING_RATE, 1e-5)}]

        # Define Optimizer
        self.optimizer = torch.optim.SGD(train_params,
                                         momentum=0.9,
                                         weight_decay=5e-4,
                                         nesterov=False)

        self.criterion = SegmentationLosses(weight=None, cuda=self.device != "cpu").build_loss()

        # Define Evaluator
        self.evaluator = Evaluator(self.num_classes)

        # Define lr scheduler
        # scheduler = LR_Scheduler(args.lr_scheduler, learning_rate,
        #                               args.epochs, len(self.train_loader))

        # print information before training start
        print("-" * 60)
        print("instructions")
        pprint(instructions)
        model_parameters = sum([p.nelement() for p in self.model.parameters()])
        print("Model parameters: {:.2E}".format(model_parameters))

        self.best_prediction = 0.0

    def train(self, epoch):
        self.model.train()
        train_loss = 0.0

        # create a progress bar
        pbar = tqdm(self.data_loader_train)
        num_batches_train = len(self.data_loader_train)

        # go through each item in the training data
        for i, sample in enumerate(pbar):
            # set input and target
            nn_input = sample[STR.NN_INPUT].to(self.device)
            nn_target = sample[STR.NN_TARGET].to(self.device, dtype=torch.float)

            # run model
            output = self.model(nn_input)

            # calc losses
            loss = self.criterion(output, nn_target)
            # # save step losses
            # combined_loss_steps.append(float(loss))
            # regression_loss_steps.append(float(regression_loss))
            # classification_loss_steps.append(float(classification_loss))

            train_loss += loss.item()
            pbar.set_description('Train loss: %.3f' % (train_loss / (i + 1)))
            self.writer.add_scalar('train/total_loss_iter', loss.item(), i + num_batches_train * epoch)

            # calculate gradient and update model weights
            loss.backward()
            # torch.nn.utils.clip_grad_norm_(model.parameters(), 0.1)
            self.optimizer.step()
            self.optimizer.zero_grad()

        self.writer.add_scalar('train/total_loss_epoch', train_loss, epoch)
        print("[Epoch: {}, num crops: {}]".format(epoch, num_batches_train * self.batch_size))

        print('Loss: %.3f' % train_loss)

    def validation(self, epoch):

        self.model.eval()
        self.evaluator.reset()
        test_loss = 0.0

        pbar = tqdm(self.data_loader_valid, desc='\r')
        num_batches_val = len(self.data_loader_train)

        for i, sample in enumerate(pbar):
            # set input and target
            nn_input = sample[STR.NN_INPUT].to(self.device)
            nn_target = sample[STR.NN_TARGET].to(self.device, dtype=torch.float)

            with torch.no_grad():
                output = self.model(nn_input)

            loss = self.criterion(output, nn_target)
            test_loss += loss.item()
            pbar.set_description('Test loss: %.3f' % (test_loss / (i + 1)))
            pred = output.data.cpu().numpy()
            pred = np.argmax(pred, axis=1)
            nn_target = nn_target.cpu().numpy()
            # Add batch sample into evaluator
            self.evaluator.add_batch(nn_target, pred)

        # Fast test during the training
        Acc = self.evaluator.Pixel_Accuracy()
        Acc_class = self.evaluator.Pixel_Accuracy_Class()
        mIoU = self.evaluator.Mean_Intersection_over_Union()
        FWIoU = self.evaluator.Frequency_Weighted_Intersection_over_Union()
        self.writer.add_scalar('val/total_loss_epoch', test_loss, epoch)
        self.writer.add_scalar('val/mIoU', mIoU, epoch)
        self.writer.add_scalar('val/Acc', Acc, epoch)
        self.writer.add_scalar('val/Acc_class', Acc_class, epoch)
        self.writer.add_scalar('val/fwIoU', FWIoU, epoch)
        print('Validation:')
        print("[Epoch: {}, num crops: {}]".format(epoch, num_batches_val * self.batch_size))
        print("Acc:{}, Acc_class:{}, mIoU:{}, fwIoU: {}".format(Acc, Acc_class, mIoU, FWIoU))
        print('Loss: %.3f' % test_loss)

        new_pred = mIoU
        is_best = new_pred > self.best_prediction
        self.saver.save_checkpoint(self.model, is_best, epoch)


if __name__ == "__main__":
    pass
