import argparse
import json
import os
import pytorch_lightning as pl
import torch
import torch.nn as nn
from torchmetrics.classification import BinaryJaccardIndex

from codebase.datasets.deepglobe import RoadsDataset
from codebase.models.dlinknet import DLinkNet34
from codebase.utils import transforms
from codebase.utils.losses import DiceLoss
from codebase.utils.metrics import BinaryAccuracy
from pytorch_lightning import LightningModule
from pytorch_lightning.callbacks import EarlyStopping, LearningRateMonitor, ModelCheckpoint
from pytorch_lightning.loggers import TensorBoardLogger
from time import strftime
from torch.utils.data import DataLoader, random_split
from torchvision.transforms import Compose


PARAMS = None


class DLinkNetModel(LightningModule):
    def __init__(self, lr=1e-3, min_lr=0.0):
        super().__init__()
        self.lr = lr
        self.min_lr = min_lr
        self.segmentation_model = DLinkNet34(backbone='imagenet')
        self.criterion1 = nn.BCELoss()
        self.criterion2 = DiceLoss()
        self.metric = BinaryJaccardIndex()

    def training_step(self, batch, batch_idx):
        loss_bce, loss_dice, accuracy = self.shared_step(batch)

        loss = loss_bce + loss_dice

        self.log("train/loss", loss, on_step=False, on_epoch=True)
        self.log("train/loss_bce", loss_bce, on_step=False, on_epoch=True)
        self.log("train/loss_dice", loss_dice, on_step=False, on_epoch=True)
        self.log("train/iou", accuracy, on_step=False, on_epoch=True)

        return loss

    def validation_step(self, batch, batch_idx):
        loss_bce, loss_dice, accuracy = self.shared_step(batch)

        loss = loss_bce + loss_dice

        self.log("valid/loss", loss, on_step=False, on_epoch=True)
        self.log("valid/loss_bce", loss_bce, on_step=False, on_epoch=True)
        self.log("valid/loss_dice", loss_dice, on_step=False, on_epoch=True)
        self.log("valid/iou", accuracy, on_step=False, on_epoch=True)

    def test_step(self, batch, batch_idx):
        images, labels = batch['image'], batch['label']

        predictions_list = []

        for img in images:
            img90 = torch.rot90(img, k=1, dims=[1, 2])
            img1 = torch.stack((img, img90))
            img2 = torch.flip(img1, dims=[2])  # Vertical flip
            img3 = torch.concatenate((img1, img2))
            img4 = torch.flip(img3, dims=[3])  # Horizontal flip
            # img5 = img3.transpose(0, 3, 1, 2)
            # img5 = np.array(img5, np.float32) / 255.0 * 3.2 - 1.6
            # img5 = V(torch.Tensor(img5).to(self.device))
            img5 = img3
            # img6 = img4.transpose(0, 3, 1, 2)
            # img6 = np.array(img6, np.float32) / 255.0 * 3.2 - 1.6
            # img6 = V(torch.Tensor(img6).to(self.device))
            img6 = img4

            pred_a = self.segmentation_model(img5)
            pred_b = self.segmentation_model(img6)

            pred1 = pred_a + torch.flip(pred_b, dims=[3])  # Revert horizontal flip
            pred2 = pred1[:2] + torch.flip(pred1[2:], dims=[2])  # Revert vertical flip
            pred3 = pred2[0] + torch.flip(torch.flip(torch.rot90(pred2[1], k=1, dims=[1, 2]), dims=[1]), dims=[2])

            pred3[pred3 > 4.0] = 255
            pred3[pred3 <= 4.0] = 0

            pred3 = pred3 / 255.0
            pred3[pred3 >= 0.5] = 1.0
            pred3[pred3 < 0.5] = 0.0

            predictions_list.append(pred3)

        predictions = torch.stack(predictions_list)

        loss_bce = self.criterion1(predictions, labels)
        loss_dice = self.criterion2(predictions, labels)

        loss = loss_bce + loss_dice

        accuracy = self.metric(predictions, labels)

        self.log("test/loss", loss, on_step=False, on_epoch=True)
        self.log("test/loss_bce", loss_bce, on_step=False, on_epoch=True)
        self.log("test/loss_dice", loss_dice, on_step=False, on_epoch=True)
        self.log("test/iou", accuracy, on_step=False, on_epoch=True)

    def shared_step(self, batch):
        images, labels = batch['image'], batch['label']

        predictions = self.segmentation_model(images)

        loss_bce = self.criterion1(predictions, labels)
        loss_dice = self.criterion2(predictions, labels)

        accuracy = self.metric(predictions, labels)

        return loss_bce, loss_dice, accuracy

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.2, patience=3,
                                                               min_lr=self.min_lr, verbose=True)
        return {"optimizer": optimizer, "lr_scheduler": {"scheduler": scheduler, "monitor": "train/loss"}}


def main():
    data_dir = PARAMS.data_dir
    results_dir = PARAMS.results_dir
    epochs = PARAMS.epochs
    batch_size = PARAMS.batch_size
    test_batch_size = 1 if batch_size // 4 == 0 else batch_size // 4
    learning_rate = PARAMS.learning_rate
    name = PARAMS.name
    test_ckpt_path = PARAMS.test_ckpt_path
    scheduler_min_lr = PARAMS.scheduler_min_lr
    min_delta = PARAMS.early_stopping_min_delta
    patience = PARAMS.early_stopping_patience

    results_dir_root = os.path.dirname(results_dir.rstrip('/'))
    results_dir_name = os.path.basename(results_dir.rstrip('/'))

    exp_name = f"{name}-{strftime('%y%m%d')}-{strftime('%H%M%S')}"

    device = "cuda" if torch.cuda.is_available() else "cpu"

    train_dataset = RoadsDataset(data_dir=data_dir,
                                 is_train=True,
                                 transform=Compose([transforms.RandomHSV(hue_shift_limit=(-30, 30),
                                                                         sat_shift_limit=(-5, 5),
                                                                         val_shift_limit=(-15, 15)),
                                                    transforms.RandomShiftScale(shift_limit=(-0.1, 0.1),
                                                                                scale_limit=(-0.1, 0.1),
                                                                                aspect_limit=(-0.1, 0.1)),
                                                    transforms.RandomHorizontalFlip(),
                                                    transforms.RandomVerticalFlip(),
                                                    transforms.RandomRotation(),
                                                    transforms.Normalize(feat_range=(-1.6, 1.6), threshold=True),
                                                    transforms.ToTensor()]))

    # use 20% of training data for validation
    train_set_size = int(len(train_dataset) * 0.8)
    valid_set_size = len(train_dataset) - train_set_size

    # split the train set into two
    seed = torch.Generator().manual_seed(42)
    train_dataset, valid_dataset = random_split(train_dataset, [train_set_size, valid_set_size], generator=seed)

    train_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=8)
    valid_dataloader = DataLoader(valid_dataset, batch_size=batch_size, shuffle=False, num_workers=8)

    test_dataset = RoadsDataset(data_dir=data_dir,
                                is_train=False,
                                transform=Compose([transforms.Normalize(feat_range=(-1.6, 1.6), threshold=True),
                                                   transforms.ToTensor()]))
    test_dataloader = DataLoader(test_dataset, batch_size=test_batch_size, shuffle=False, num_workers=8)

    # Initialize model
    roads_model = DLinkNetModel(lr=learning_rate, min_lr=scheduler_min_lr)

    # Initialize logger
    logger = TensorBoardLogger(save_dir=results_dir_root, name=results_dir_name, version=exp_name,
                               default_hp_metric=False, sub_dir="logs")

    # Dump program arguments
    logger.log_hyperparams(params=PARAMS)

    # Initialize callbacks
    early_stopping = EarlyStopping(monitor="train/loss", min_delta=min_delta, patience=patience, verbose=True)
    lr_monitor = LearningRateMonitor(logging_interval="epoch")
    checkpointing = ModelCheckpoint(monitor="train/loss", save_top_k=5, mode="min")

    # Initialize trainer
    trainer = pl.Trainer(logger=logger, callbacks=[early_stopping, lr_monitor, checkpointing],
                         enable_progress_bar=False, max_epochs=epochs, accelerator=device)

    # Perform training
    if not test_ckpt_path:
        trainer.fit(model=roads_model, train_dataloaders=train_dataloader, val_dataloaders=valid_dataloader)

    # Perform evaluation
    trainer.test(model=roads_model, dataloaders=test_dataloader, ckpt_path=test_ckpt_path)


def parse_args():
    parser = argparse.ArgumentParser(description="Trainer for road extraction model")
    parser.add_argument("--data_dir", help="Dataset directory", required=True)
    parser.add_argument("--results_dir", help="Results directory", default="./results/")
    parser.add_argument("--epochs", help="Number of epochs", type=int, default=300)
    parser.add_argument("--batch_size", help="Batch size", type=int, required=True)
    parser.add_argument("--learning_rate", help="Learning rate", type=float, default=0.0002)
    parser.add_argument("--name", help="Model name", default="dlinknet34")
    parser.add_argument("--test_ckpt_path", help="Test checkpoint path")
    parser.add_argument("--scheduler_min_lr", help="Scheduler minimum learning rate", type=float, default=0.0)
    parser.add_argument("--early_stopping_min_delta", help="Min early stopping difference", type=float, default=0.002)
    parser.add_argument("--early_stopping_patience", help="Patience for early stopping", type=int, default=6)
    return parser.parse_args()


if __name__ == "__main__":
    PARAMS = parse_args()
    main()
