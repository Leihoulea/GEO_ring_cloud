"""The prescribed fully convolutional CNN: no pooling, attention, or resizing."""
from __future__ import annotations


def build_cnn(input_channels: int):
    import torch.nn as nn
    return nn.Sequential(
        nn.Conv2d(input_channels, 64, 3, padding=1), nn.ReLU(),
        nn.Conv2d(64, 64, 3, padding=1), nn.ReLU(),
        nn.Conv2d(64, 64, 3, padding=1), nn.ReLU(),
        nn.Conv2d(64, 64, 3, padding=1), nn.ReLU(),
        nn.Conv2d(64, 32, 3, padding=1), nn.ReLU(),
        nn.Conv2d(32, 1, 3, padding=1),
    )
