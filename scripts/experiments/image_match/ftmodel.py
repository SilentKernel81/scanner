"""
The fine-tuned embedding network: MobileNetV3-Large (ImageNet start) with a head that KEEPS WHERE THINGS ARE.

The pretrained networks failed at matching cards because global average pooling throws position away and every card shares one
frame layout. Here the last feature map (960 channels, 10 x 7 cells for a 312 x 224 picture) is cut to 32 channels by a 1 x 1
convolution, flattened (32 x 10 x 7 = 2,240 numbers, position intact) and mapped to 128 numbers by one linear layer. Only
operations OpenCV's dnn module runs (convolution, hard-swish, flatten, matrix product), like corner_net.
"""
import torch
import torch.nn as nn
import torch.utils.checkpoint
import torchvision

EMBEDDING = 128
INPUT_W, INPUT_H = 224, 312


class FtNet(nn.Module):
    def __init__(self, pretrained=True):
        super().__init__()
        backbone = torchvision.models.mobilenet_v3_large(weights='IMAGENET1K_V2' if pretrained else None)
        self.features = backbone.features
        self.reduce = nn.Sequential(nn.Conv2d(960, 32, 1, bias=False), nn.BatchNorm2d(32), nn.ReLU(inplace=True))
        with torch.no_grad():
            cells = self.reduce(self.features(torch.zeros(1, 3, INPUT_H, INPUT_W))).numel()
        self.flat = nn.Flatten()
        self.drop = nn.Dropout(0.1)
        self.embed = nn.Linear(cells, EMBEDDING)

    def forward(self, x):
        if self.training:
            # training only: recompute the backbone's activations in the backward pass instead of keeping them (the laptop GPU's memory)
            maps = torch.utils.checkpoint.checkpoint_sequential(self.features, 4, x, use_reentrant=False)
        else:
            maps = self.features(x)
        return self.embed(self.drop(self.flat(self.reduce(maps))))      # not normalised: the caller does (cosine)
