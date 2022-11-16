# MoCo

论文地址：

https://arxiv.org/pdf/1911.05722.pdf

主要用于自我学习和整理，具体代码请见：

源代码：https://colab.research.google.com/github/facebookresearch/moco/blob/colab-notebook/colab/moco_cifar10_demo.ipynb

对比学习——cifar10数据集

这里的对称和不对称主要体现在对比损失函数上。

以下实验结果都是对比损失是非对称的：args.symmetric = False；

分类采用的是KNN分类：

| 对比损失是否对称               | V1版本（使用了cos学习率下降） | 使用映射头（MLP） | 使用映射头+Gaussian Deblur数据增强策略 |
| ---------------------- | ----------------- | ---------- | --------------------------- |
| **Asymmetrical（不对称的）** | 83.11%            | 82.78%     |                             |

在参数设置中分别设置：

迭代次数都是epoch=200

① args.cos = True

② args.cos = True   +    args.mlp = True

③ args.cos = True   +    args.mlp = True    +     args.aug_plus = True

特别要注意这里，在cifar数据集中采用的ResNet18，源代码重新写过了ResNet18的结构

![](C:\Users\Administrator\AppData\Roaming\marktext\images\2022-11-14-17-31-56-image.png)
