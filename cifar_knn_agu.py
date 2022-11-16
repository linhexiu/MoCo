import os
from datetime import datetime
from functools import partial
from PIL import Image
from torch.utils.data import DataLoader
from torchvision import transforms
from torchvision.datasets import CIFAR10
from torchvision.models import resnet
from tqdm import tqdm
import argparse
import json
import math
import os
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import ImageFilter
import random
import warnings
import torch.multiprocessing as mp
import builtins
from torch.nn.parallel import DistributedDataParallel
import torch.distributed as dist

parser = argparse.ArgumentParser(description='Train MoCo on CIFAR-10')

parser.add_argument('-a', '--arch', default='resnet18')

# lr: 0.06 for batch 512 (or 0.03 for batch 256)
parser.add_argument('--lr', '--learning-rate', default=0.06, type=float, metavar='LR', help='initial learning rate',
                    dest='lr')
parser.add_argument('--epochs', default=200, type=int, metavar='N', help='number of total epochs to run')
parser.add_argument('--schedule', default=[120, 160], nargs='*', type=int,
                    help='learning rate schedule (when to drop lr by 10x); does not take effect if --cos is on')

"""
V2版本添加映射头、数据增强使用了Gaussian Deblur、使用与cos学习率下降
"""
parser.add_argument('--mlp', action='store_true',
                    help='use mlp head')
parser.add_argument('--aug-plus', action='store_true',
                    help='use moco v2 data augmentation')
parser.add_argument('--cos', action='store_true',
                    help='use cosine lr schedule')

parser.add_argument('--batch-size', default=512, type=int, metavar='N', help='mini-batch size')
parser.add_argument('--wd', default=5e-4, type=float, metavar='W', help='weight decay')

# moco specific configs:
parser.add_argument('--moco-dim', default=128, type=int, help='feature dimension')
parser.add_argument('--moco-k', default=4096, type=int, help='queue size; number of negative keys')
parser.add_argument('--moco-m', default=0.99, type=float, help='moco momentum of updating key encoder')
parser.add_argument('--moco-t', default=0.1, type=float, help='softmax temperature')

parser.add_argument('--bn-splits', default=8, type=int,
                    help='simulate multi-gpu behavior of BatchNorm in one gpu; 1 is SyncBatchNorm in multi-gpu')

parser.add_argument('--symmetric', action='store_true',
                    help='use a symmetric loss function that backprops to both crops')

# knn monitor
parser.add_argument('--knn-k', default=200, type=int, help='k in kNN monitor')
parser.add_argument('--knn-t', default=0.1, type=float,
                    help='softmax temperature in kNN monitor; could be different with moco-t')

# utils
parser.add_argument('--resume', default='', type=str, metavar='PATH', help='path to latest checkpoint (default: none)')
parser.add_argument('--results-dir', default='', type=str, metavar='PATH', help='path to cache (default: none)')


class CIFAR10Pair(CIFAR10):
    """CIFAR10 Dataset.
    """

    def __getitem__(self, index):
        img = self.data[index]
        img = Image.fromarray(img)

        if self.transform is not None:
            im_1 = self.transform(img)
            im_2 = self.transform(img)

        return im_1, im_2


class GaussianBlur(object):
    """Gaussian blur augmentation in SimCLR https://arxiv.org/abs/2002.05709"""

    def __init__(self, sigma=[.1, 2.]):
        self.sigma = sigma

    def __call__(self, x):
        sigma = random.uniform(self.sigma[0], self.sigma[1])
        x = x.filter(ImageFilter.GaussianBlur(radius=sigma))
        return x


def dist_setup(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'  # 由于本次是单机，故MASTER_ADDR写成localhost即可
    os.environ['MASTER_PORT'] = '10000'
    dist.init_process_group(backend='nccl', world_size=world_size, rank=rank)
    # rank表示进程编号。rank这个参数是由进程控制的，不用显性设置


def main(rank, world_size, *args):
    args = parser.parse_args()
    # 改变batch_size
    args.batch_size = int(args.batch_size / world_size)  # 512->256 每块有256
    print(args.batch_size)

    dist_setup(rank, world_size)

    # set command line arguments here when running in ipynb
    args.epochs = 200

    # V2版本
    args.cos = True
    args.mlp = True
    args.aug_plus = True

    args.schedule = []  # cos in use
    args.symmetric = False
    if args.results_dir == '':
        args.results_dir = './cache-' + datetime.now().strftime("%Y-%m-%d-%H-%M-%S-moco")

    # print(args)
    # dataloader

    if args.aug_plus:
        # MoCo v2's aug: similar to SimCLR https://arxiv.org/abs/2002.05709
        train_transform = transforms.Compose([
            transforms.RandomResizedCrop(224, scale=(0.2, 1.)),
            transforms.RandomApply([
                transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)  # not strengthened
            ], p=0.8),
            transforms.RandomGrayscale(p=0.2),
            transforms.RandomApply([GaussianBlur([.1, 2.])], p=0.5),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize([0.4914, 0.4822, 0.4465], [0.2023, 0.1994, 0.2010])])
    else:
        train_transform = transforms.Compose([
            transforms.RandomResizedCrop(32),
            transforms.RandomHorizontalFlip(p=0.5),
            transforms.RandomApply([transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)], p=0.8),
            transforms.RandomGrayscale(p=0.2),
            transforms.ToTensor(),
            transforms.Normalize([0.4914, 0.4822, 0.4465], [0.2023, 0.1994, 0.2010])])

    test_transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize([0.4914, 0.4822, 0.4465], [0.2023, 0.1994, 0.2010])])

    # data prepare
    train_data = CIFAR10Pair(root='data', train=True, transform=train_transform, download=False)

    sampler = torch.utils.data.DistributedSampler(train_data, num_replicas=world_size,
                                                  rank=dist.get_rank())

    train_loader = DataLoader(train_data, batch_size=args.batch_size, sampler=sampler, num_workers=16, pin_memory=True,
                              drop_last=True)

    memory_data = CIFAR10(root='data', train=True, transform=test_transform, download=False)
    memory_loader = DataLoader(memory_data, batch_size=args.batch_size, shuffle=False, num_workers=16, pin_memory=True)

    test_data = CIFAR10(root='data', train=False, transform=test_transform, download=False)
    test_loader = DataLoader(test_data, batch_size=args.batch_size, shuffle=False, num_workers=16, pin_memory=True)

    # model

    @torch.no_grad()
    def concat_all_gather(tensor):
        """
        Performs all_gather operation on the provided tensors.
        *** Warning ***: torch.distributed.all_gather has no gradient.
        """
        tensors_gather = [torch.ones_like(tensor)
                          for _ in range(torch.distributed.get_world_size())]
        torch.distributed.all_gather(tensors_gather, tensor, async_op=False)

        output = torch.cat(tensors_gather, dim=0)
        return output

    class ModelBase(nn.Module):
        """
        Common CIFAR ResNet recipe.
        Comparing with ImageNet ResNet recipe, it:
        (i) replaces conv1 with kernel=3, str=1
        (ii) removes pool1
        """

        def __init__(self, feature_dim=128, arch=None):
            super(ModelBase, self).__init__()

            # use split batchnorm
            resnet_arch = getattr(resnet, arch)
            net = resnet_arch(num_classes=feature_dim)

            self.net = []
            for name, module in net.named_children():
                if name == 'conv1':
                    module = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
                if isinstance(module, nn.MaxPool2d):
                    continue
                if isinstance(module, nn.Linear):
                    # # V1 版本
                    # self.net.append(nn.Flatten(1))

                    # V2 版本
                    self.net.append(nn.Flatten(1))
                    self.fc = module
                    self.net.append(self.fc)
                    continue
                self.net.append(module)

            self.net = nn.Sequential(*self.net)

        def forward(self, x):
            x = self.net(x)
            # note: not normalized here
            return x

    class ModelMoCo(nn.Module):
        def __init__(self, dim=128, K=4096, m=0.99, T=0.1, arch='resnet18', symmetric=True, mlp=True):
            super(ModelMoCo, self).__init__()

            self.K = K
            self.m = m
            self.T = T
            self.symmetric = symmetric

            # create the encoders
            self.encoder_q = ModelBase(feature_dim=dim, arch=arch)
            self.encoder_k = ModelBase(feature_dim=dim, arch=arch)

            # V2 版本
            if mlp:  # hack: brute-force replacement
                dim_mlp = self.encoder_q.fc.weight.shape[1]
                self.encoder_q.fc = nn.Sequential(nn.Linear(dim_mlp, dim_mlp), nn.ReLU(), self.encoder_q.fc)
                self.encoder_k.fc = nn.Sequential(nn.Linear(dim_mlp, dim_mlp), nn.ReLU(), self.encoder_k.fc)

            for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
                param_k.data.copy_(param_q.data)  # initialize
                param_k.requires_grad = False  # not update by gradient

            # create the queue
            self.register_buffer("queue", torch.randn(dim, K))
            self.queue = nn.functional.normalize(self.queue, dim=0)

            self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))

        @torch.no_grad()
        def _momentum_update_key_encoder(self):
            """
            Momentum update of the key encoder
            """
            for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
                param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)

        @torch.no_grad()
        def _dequeue_and_enqueue(self, keys):
            batch_size = keys.shape[0]

            ptr = int(self.queue_ptr)
            assert self.K % batch_size == 0  # for simplicity

            # replace the keys at ptr (dequeue and enqueue)
            self.queue[:, ptr:ptr + batch_size] = keys.t()  # transpose
            ptr = (ptr + batch_size) % self.K  # move pointer

            self.queue_ptr[0] = ptr

        @torch.no_grad()
        def _batch_unshuffle_ddp(self, x, idx_unshuffle):
            """
            Undo batch shuffle.
            *** Only support DistributedDataParallel (DDP) model. ***
            """
            # gather from all gpus
            batch_size_this = x.shape[0]
            x_gather = concat_all_gather(x)
            batch_size_all = x_gather.shape[0]

            num_gpus = batch_size_all // batch_size_this

            # restored index for this gpu
            gpu_idx = torch.distributed.get_rank()
            idx_this = idx_unshuffle.view(num_gpus, -1)[gpu_idx]

            return x_gather[idx_this]

        @torch.no_grad()
        def _batch_shuffle_ddp(self, x):
            """
            Batch shuffle, for making use of BatchNorm.
            *** Only support DistributedDataParallel (DDP) model. ***
            """
            # gather from all gpus
            batch_size_this = x.shape[0]
            x_gather = concat_all_gather(x)
            batch_size_all = x_gather.shape[0]

            num_gpus = batch_size_all // batch_size_this

            # random shuffle index
            idx_shuffle = torch.randperm(batch_size_all).cuda()

            # broadcast to all gpus
            torch.distributed.broadcast(idx_shuffle, src=0)

            # index for restoring
            idx_unshuffle = torch.argsort(idx_shuffle)

            # shuffled index for this gpu
            gpu_idx = torch.distributed.get_rank()
            idx_this = idx_shuffle.view(num_gpus, -1)[gpu_idx]

            return x_gather[idx_this], idx_unshuffle

        def contrastive_loss(self, im_q, im_k):
            # compute query features
            q = self.encoder_q(im_q)  # queries: NxC
            q = nn.functional.normalize(q, dim=1)  # already normalized

            # compute key features
            with torch.no_grad():  # no gradient to keys
                # shuffle for making use of BN
                im_k_, idx_unshuffle = self._batch_shuffle_single_gpu(im_k)

                k = self.encoder_k(im_k_)  # keys: NxC
                k = nn.functional.normalize(k, dim=1)  # already normalized

                # undo shuffle
                k = self._batch_unshuffle_single_gpu(k, idx_unshuffle)

            # compute logits
            # Einstein sum is more intuitive
            # positive logits: Nx1
            l_pos = torch.einsum('nc,nc->n', [q, k]).unsqueeze(-1)
            # negative logits: NxK
            l_neg = torch.einsum('nc,ck->nk', [q, self.queue.clone().detach()])

            # logits: Nx(1+K)
            logits = torch.cat([l_pos, l_neg], dim=1)

            # apply temperature
            logits /= self.T

            # labels: positive key indicators
            labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()

            loss = nn.CrossEntropyLoss().cuda()(logits, labels)

            return loss, q, k

        def forward(self, im1, im2):
            """
            Input:
                im_q: a batch of query images
                im_k: a batch of key images
            Output:
                loss
            """

            # update the key encoder
            with torch.no_grad():  # no gradient to keys
                self._momentum_update_key_encoder()

            # compute loss
            if self.symmetric:  # asymmetric loss
                loss_12, q1, k2 = self.contrastive_loss(im1, im2)
                loss_21, q2, k1 = self.contrastive_loss(im2, im1)
                loss = loss_12 + loss_21
                k = torch.cat([k1, k2], dim=0)
            else:  # asymmetric loss
                loss, q, k = self.contrastive_loss(im1, im2)

            self._dequeue_and_enqueue(k)

            return loss

    # create model
    model = ModelMoCo(
        dim=args.moco_dim,
        K=args.moco_k,
        m=args.moco_m,
        T=args.moco_t,
        arch=args.arch,
        symmetric=args.symmetric,
        mlp=args.mlp,
    )
    model = model.to(rank)
    model = DistributedDataParallel(model, device_ids=[rank], find_unused_parameters=True)

    # train for one epoch
    def train(net, data_loader, train_optimizer, epoch, args):
        net.train()
        adjust_learning_rate(optimizer, epoch, args)
        data_loader.sampler.set_epoch(epoch)
        total_loss, total_num, train_bar = 0.0, 0, tqdm(data_loader)
        for im_1, im_2 in train_bar:
            im_1, im_2 = im_1.cuda(rank), im_2.cuda(rank)

            loss = net(im_1, im_2)

            train_optimizer.zero_grad()
            loss.backward()
            train_optimizer.step()

            total_num += data_loader.batch_size
            total_loss += loss.item() * data_loader.batch_size
            train_bar.set_description(
                'Train Epoch: [{}/{}], lr: {:.6f}, Loss: {:.4f}'.format(epoch, args.epochs,
                                                                        optimizer.param_groups[0]['lr'],
                                                                        total_loss / total_num))

        return total_loss / total_num

    # lr scheduler for training
    def adjust_learning_rate(optimizer, epoch, args):
        """Decay the learning rate based on schedule"""
        lr = args.lr
        if args.cos:  # cosine lr schedule
            lr *= 0.5 * (1. + math.cos(math.pi * epoch / args.epochs))
        else:  # stepwise lr schedule
            for milestone in args.schedule:
                lr *= 0.1 if epoch >= milestone else 1.
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

    # test using a knn monitor
    def test(net, memory_data_loader, test_data_loader, epoch, args):
        net.eval()
        classes = len(memory_data_loader.dataset.classes)
        total_top1, total_top5, total_num, feature_bank = 0.0, 0.0, 0, []
        with torch.no_grad():
            # generate feature bank
            for data, target in tqdm(memory_data_loader, desc='Feature extracting'):
                feature = net(data.cuda(rank))
                feature = F.normalize(feature, dim=1)
                feature_bank.append(feature)
            # [D, N]
            feature_bank = torch.cat(feature_bank, dim=0).t().contiguous()
            # [N]
            feature_labels = torch.tensor(memory_data_loader.dataset.targets, device=feature_bank.device)
            # loop test data to predict the label by weighted knn search
            test_bar = tqdm(test_data_loader)
            for data, target in test_bar:
                data, target = data.cuda(rank), target.cuda(rank)
                feature = net(data)
                feature = F.normalize(feature, dim=1)

                pred_labels = knn_predict(feature, feature_bank, feature_labels, classes, args.knn_k, args.knn_t)

                total_num += data.size(0)
                total_top1 += (pred_labels[:, 0] == target).float().sum().item()
                test_bar.set_description(
                    'Test Epoch: [{}/{}] Acc@1:{:.2f}%'.format(epoch, args.epochs, total_top1 / total_num * 100))

        return total_top1 / total_num * 100

    # knn monitor as in InstDisc https://arxiv.org/abs/1805.01978
    # implementation follows http://github.com/zhirongw/lemniscate.pytorch and https://github.com/leftthomas/SimCLR
    def knn_predict(feature, feature_bank, feature_labels, classes, knn_k, knn_t):
        # compute cos similarity between each feature vector and feature bank ---> [B, N]
        sim_matrix = torch.mm(feature, feature_bank)
        # [B, K]
        sim_weight, sim_indices = sim_matrix.topk(k=knn_k, dim=-1)
        # [B, K]
        # t=feature_labels.expand(feature.size(0), -1)
        sim_labels = torch.gather(feature_labels.expand(feature.size(0), -1), dim=-1, index=sim_indices)
        sim_weight = (sim_weight / knn_t).exp()

        # counts for each class
        one_hot_label = torch.zeros(feature.size(0) * knn_k, classes, device=sim_labels.device)
        # [B*K, C]
        one_hot_label = one_hot_label.scatter(dim=-1, index=sim_labels.view(-1, 1), value=1.0)
        # weighted score ---> [B, C]
        pred_scores = torch.sum(one_hot_label.view(feature.size(0), -1, classes) * sim_weight.unsqueeze(dim=-1), dim=1)

        pred_labels = pred_scores.argsort(dim=-1, descending=True)
        return pred_labels

    # define optimizer
    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, weight_decay=args.wd, momentum=0.9)

    # load model if resume
    epoch_start = 1
    if args.resume != '':
        checkpoint = torch.load(args.resume, map_location=torch.device("cuda:0"))
        model.load_state_dict(checkpoint['state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        epoch_start = checkpoint['epoch'] + 1
        print('Loaded from: {}'.format(args.resume))

    # logging
    results = {'train_loss': [], 'test_acc@1': []}
    if not os.path.exists(args.results_dir):
        os.mkdir(args.results_dir)

    if rank == 0:
        # dump args
        with open(args.results_dir + '/args.json', 'w') as fid:
            json.dump(args.__dict__, fid, indent=2)

    # training loop
    for epoch in range(epoch_start, args.epochs + 1):
        train_loss = train(model, train_loader, optimizer, epoch, args)
        results['train_loss'].append(train_loss)
        test_acc_1 = test(model.encoder_q, memory_loader, test_loader, epoch, args)
        results['test_acc@1'].append(test_acc_1)
        # save statistics
        data_frame = pd.DataFrame(data=results, index=range(epoch_start, epoch + 1))
        data_frame.to_csv(args.results_dir + '/log.csv', index_label='epoch')
        # save model： 只在一个GPU上进行保存即可
        if rank == 0:
            torch.save({'epoch': epoch, 'state_dict': model.module.state_dict(), 'optimizer': optimizer.state_dict(), },
                       args.results_dir + '/model_last.pth')


if __name__ == '__main__':
    os.environ['CUDA_VISIBLE_DEVICE'] = '0,1,2,3'
    world_size = 4  # 进程数，要与cuda_visible_devices的数量一致
    mp.spawn(main, args=(world_size,), nprocs=world_size, join=True)
