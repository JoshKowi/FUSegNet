import torch
from torch.utils.data import DataLoader
from torch.utils.data import Dataset as BaseDataset
import albumentations as A
import cv2
import numpy as np
import segmentation_models_pytorch as smp
from segmentation_models_pytorch.utils import metrics, losses, base
import random
import os
from datetime import datetime
from copy import deepcopy
import pickle
from torchsummary import summary 
import matplotlib.pyplot as plt

"""## Dataloader"""
class Dataset(BaseDataset):
    """ Reference: https://github.com/qubvel/segmentation_models.pytorch
    
    Args:
        list_IDs (list): List of image names with extension
        images_dir (str): path to images folder
        masks_dir (str): path to segmentation masks folder
        augmentation (albumentations.Compose): data transfromation pipeline 
            (e.g. flip, scale, etc.)
        preprocessing (albumentations.Compose): data preprocessing 
            (e.g. noralization, shape manipulation, etc.)
    
    """
    
    def __init__(
            self, 
            list_IDs,
            images_dir, 
            masks_dir, 
            augmentation=None, 
            preprocessing=None,
    ):
        self.ids = list_IDs
        self.images_fps = [os.path.join(images_dir, image_id) for image_id in self.ids]
        self.masks_fps = [os.path.join(masks_dir, image_id) for image_id in self.ids]
              
        self.augmentation = augmentation
        self.preprocessing = preprocessing
    
    def __getitem__(self, i):
        
        # read data
        image = cv2.imread(self.images_fps[i])
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        mask = cv2.imread(self.masks_fps[i], 0)    # ----------------- pay attention ------------------ #
        mask = mask/255.0   # converting mask to (0 and 1) # ----------------- pay attention ------------------ #
        mask = np.expand_dims(mask, axis=-1)  # adding channel axis # ----------------- pay attention ------------------ #
        
        # apply augmentations
        if self.augmentation:
            sample = self.augmentation(image=image, mask=mask)
            image, mask = sample['image'], sample['mask']
        
        # apply preprocessing
        if self.preprocessing:
            sample = self.preprocessing(image=image, mask=mask)
            image, mask = sample['image'], sample['mask']
            
        return image, mask
        
    def __len__(self):
        return len(self.ids)

"""## Augmentation"""

def get_training_augmentation():
    train_transform = [

        A.OneOf(
            [
                A.HorizontalFlip(p=0.8),
                A.VerticalFlip(p=0.4),
            ],
            p=0.5,
        ),
        
        A.OneOf(
            [
                A.ShiftScaleRotate(scale_limit=0.5, rotate_limit=0, shift_limit=0, p=1, border_mode=0), # scale only
                A.ShiftScaleRotate(scale_limit=0, rotate_limit=30, shift_limit=0, p=1, border_mode=0), # rotate only
                A.ShiftScaleRotate(scale_limit=0, rotate_limit=0, shift_limit=0.1, p=1, border_mode=0), # shift only
                A.ShiftScaleRotate(scale_limit=0.5, rotate_limit=30, shift_limit=0.1, p=1, border_mode=0), # affine transform
            ],
            p=0.9,
        ),


        A.OneOf(
            [
                A.Perspective(p=1),
                A.GaussNoise(p=1),
                A.Sharpen(p=1),
                A.Blur(blur_limit=3, p=1),
                A.MotionBlur(blur_limit=3, p=1),
            ],
            p=0.2,
        ),

        A.OneOf(
            [
                A.CLAHE(p=1),
                A.RandomBrightnessContrast(brightness_limit=0.2, contrast_limit=0.2, p=1),
                A.RandomGamma(p=1),
                A.HueSaturationValue(p=1),
            ],
            p=0.2,
        ),
        
    ]

    return A.Compose(train_transform, p=0.9) # 90% augmentation probability


def get_validation_augmentation():
    """Add paddings to make image shape divisible by 32"""
    test_transform = [
        # A.PadIfNeeded(512, 512)
    ]
    return A.Compose(test_transform)


def to_tensor(x, **kwargs):
    return x.transpose(2, 0, 1).astype('float32')


def get_preprocessing(preprocessing_fn):
    """Construct preprocessing transform
    
    Args:
        preprocessing_fn (callbale): data normalization function 
            (can be specific for each pretrained neural network)
    Return:
        transform: albumentations.Compose
    
    """
    
    _transform = [
        A.Lambda(image=preprocessing_fn),
        A.Lambda(image=to_tensor, mask=to_tensor),
    ]
    return A.Compose(_transform)



"""## Split dataset"""

#%% Load dataset
x_train_dir = x_valid_dir = 'dataset/train/images'
y_train_dir = y_valid_dir = 'dataset/train/labels'

x_test_dir = 'dataset/test/images'
y_test_dir = 'dataset/test/labels'

names = os.listdir(x_train_dir)

n_val = int(len(names) * 0.15)      # 15% for validation

n_train = len(names) - n_val

random.seed(42) # seed for random number generator 

random.shuffle(names) # shuffle names

list_IDs_train = names[:n_train]
list_IDs_val = names[n_train:n_train+n_val]
list_IDs_test = os.listdir(x_test_dir)

print('No. of training images: ', n_train)
print('No. of validation images: ', n_val)
print('No. of training images: ', len(list_IDs_test))

#%%
"""## Parameters"""
# Parameters
BASE_MODEL = 'FuSegNet'
ENCODER = 'efficientnet-b7'
ENCODER_WEIGHTS = 'imagenet'
BATCH_SIZE = 8
IMAGE_SIZE = 224 # height and width
n_classes = 1 
ACTIVATION = 'sigmoid' # could be None for logits or 'softmax2d' for multiclass segmentation
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
LR = 0.0001 # learning rate
EPOCHS = 200
WEIGHT_DECAY = 1e-5
SAVE_WEIGHTS_ONLY = True
TO_CATEGORICAL = False
SAVE_BEST_MODEL = True
SAVE_LAST_MODEL = False
PERIOD = None # periodically save checkpoints
RAW_PREDICTION = False # if true, then stores raw predictions (i.e. before applying threshold)
PATIENCE = 30 # for early stopping
EARLY_STOP = True

# Create a unique model name
model_name = BASE_MODEL + '_' + ENCODER + '_' + datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
print(model_name)

"""# Build model"""

import ssl
ssl._create_default_https_context = ssl._create_unverified_context

# Checkpoint directory
checkpoint_loc = 'checkpoints/' + model_name

# Create checkpoint directory if does not exist
if not os.path.exists(checkpoint_loc): os.makedirs(checkpoint_loc)

#%% Helper function: save a model

def save(model_path, epoch, model_state_dict, optimizer_state_dict):
    
    state = {
        'epoch': epoch + 1,
        'state_dict': deepcopy(model_state_dict),
        'optimizer': deepcopy(optimizer_state_dict),
        }
    
    torch.save(state, model_path)

#%% Loss and metrics
# Loss function
dice_loss = losses.DiceLoss()
focal_loss = losses.FocalLoss() 

total_loss = base.SumOfLosses(dice_loss, focal_loss)

# Metrics
metrics = [
    metrics.IoU(threshold=0.5),
    metrics.Fscore(threshold=0.5),
]

#%% Build model 
model = smp.Unet(
    encoder_name=ENCODER, 
    encoder_weights=ENCODER_WEIGHTS,    
    classes=n_classes, 
    activation=ACTIVATION,
    decoder_attention_type = 'pscse',
)

preprocessing_fn = smp.encoders.get_preprocessing_fn(ENCODER, ENCODER_WEIGHTS)

model.to(DEVICE)

# Model summary
summary(model, (3, IMAGE_SIZE, IMAGE_SIZE))

# Optimizer
optimizer = torch.optim.Adam([ 
    dict(params=model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY),
])
# Learning rate scheduler
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 
                              factor=0.1,
                              mode='min',
                              patience=10,
                              min_lr=0.00001,
                              verbose=True,
                              )

#%%
"""# Dataloader"""

train_dataset = Dataset(
    list_IDs_train,
    x_train_dir, 
    y_train_dir, 
    augmentation=get_training_augmentation(), 
    preprocessing=get_preprocessing(preprocessing_fn),
)

valid_dataset = Dataset(
    list_IDs_val,
    x_valid_dir, 
    y_valid_dir, 
    augmentation=get_validation_augmentation(), 
    preprocessing=get_preprocessing(preprocessing_fn),
)

train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True, num_workers=4)
valid_loader = DataLoader(valid_dataset, batch_size=BATCH_SIZE, shuffle=False, num_workers=4)

#%%
"""## Training"""
# create epoch runners 
# it is a simple loop of iterating over dataloader`s samples
train_epoch = smp.utils.train.TrainEpoch(
    model, 
    loss=total_loss, 
    metrics=metrics, 
    optimizer=optimizer,
    device=DEVICE,
    verbose=True,
)

valid_epoch = smp.utils.train.ValidEpoch(
    model, 
    loss=total_loss, 
    metrics=metrics, 
    device=DEVICE,
    verbose=True,
)

# train model for N epochs
best_viou = 0.0    
best_vloss = 1_000_000.    
save_model = False    
cnt_patience = 0

store_train_loss, store_val_loss = [], []
store_train_iou, store_val_iou = [], []
store_train_dice, store_val_dice = [], []

for epoch in range(EPOCHS):
    
    print('\nEpoch: {}'.format(epoch))
    train_logs = train_epoch.run(train_loader)
    valid_logs = valid_epoch.run(valid_loader)
    
    # Store losses and metrics
    train_loss_key = list(train_logs.keys())[0] # first key is for loss
    val_loss_key = list(valid_logs.keys())[0] # first key is for loss
    
    store_train_loss.append(train_logs[train_loss_key])
    store_val_loss.append(valid_logs[val_loss_key])
    store_train_iou.append(train_logs["iou_score"])
    store_val_iou.append(valid_logs["iou_score"])
    store_train_dice.append(train_logs["fscore"])
    store_val_dice.append(valid_logs["fscore"])
    
    # Track best performance, and save the model's state
    if  best_vloss > valid_logs[val_loss_key]:
        best_vloss = valid_logs[val_loss_key]
        print(f'Validation loss reduced. Saving the model at epoch: {epoch:04d}')
        cnt_patience = 0 # reset patience
        best_model_epoch = epoch
        save_model = True
    
    # Compare iou score
    elif best_viou < valid_logs['iou_score']:
        best_viou = valid_logs['iou_score']
        print(f'Validation IoU increased. Saving the model at epoch: {epoch:04d}.')
        cnt_patience = 0 # reset patience
        best_model_epoch = epoch
        save_model = True
        
    else: cnt_patience += 1

    # Learning rate scheduler
    scheduler.step(valid_logs[sorted(valid_logs.keys())[0]]) # monitor validation loss
    
    # Save the model
    if save_model:
        save(os.path.join(checkpoint_loc, 'best_model' + '.pth'), 
              epoch+1, model.state_dict(), optimizer.state_dict())
        save_model = False
    
    # Early stopping
    if EARLY_STOP and cnt_patience >= PATIENCE: 
      print(f"Early stopping at epoch: {epoch:04d}")
      break 

    # Periodic checkpoint save
    if not SAVE_BEST_MODEL and PERIOD is not None:
      if (epoch+1) % PERIOD == 0:
        save(os.path.join(checkpoint_loc, f"cp-{epoch+1:04d}.pth"), 
              epoch+1, model.state_dict(), optimizer.state_dict())
        print(f'Checkpoint saved for epoch {epoch:04d}')

if not EARLY_STOP and SAVE_LAST_MODEL:
    print('Saving last model')
    save(os.path.join(checkpoint_loc, 'last_model' + '.pth'), 
          epoch+1, model.state_dict(), optimizer.state_dict())

# sorted(valid_logs.keys())

"""## Plotting """
fig, ax = plt.subplots(3,1, figsize=(7, 14))

ax[0].plot(store_train_loss, 'r')
ax[0].plot(store_val_loss, 'b')
ax[0].set_title('Loss curve')
ax[0].legend(['training', 'validation'])

ax[1].plot(store_train_iou, 'r')
ax[1].plot(store_val_iou, 'b')
ax[1].set_title('IoU curve')
ax[1].legend(['training', 'validation'])

ax[2].plot(store_train_iou, 'r')
ax[2].plot(store_val_iou, 'b')
ax[2].set_title('Dice curve')
ax[2].legend(['training', 'validation'])

fig.tight_layout()

# plt.show()

save_fig_dir = "plots"
if not os.path.exists(save_fig_dir): os.makedirs(save_fig_dir)

fig.savefig(os.path.join(save_fig_dir, model_name + '.png'))
