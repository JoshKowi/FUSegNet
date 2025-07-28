## Training FuSeg-Net for CPT-Images

FuSeg-Net was originally created by the Milwaukee Group and M. Dhar (see [original paper](https://arxiv.org/abs/2305.02961)).

To adapt it to our use case, xfusegnet was first trained on the original data (complete FuSeg-Images, not only wound-patches) and afterwards fine-tuned for 20 epochs on manually labeled CPT-Images.

The weight-files of the original and fine-tuned xfusegnet can be found at [datashare.tu-dresden.de](https://datashare.tu-dresden.de/s/GmdD3d3q3mByWF5).

For details on the training and model architecture see [original_README](original_README.md).