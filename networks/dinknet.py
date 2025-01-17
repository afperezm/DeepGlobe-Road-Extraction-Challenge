"""
Based on https://github.com/zlckanata/DeepGlobe-Road-Extraction-Challenge
"""
import os
import torch
import torch.nn as nn
import torch.nn.functional as F

from copy import deepcopy
from functools import partial

from model.attention.CBAM import CBAMBlock

from models.moco2_module import MocoV2
from networks.attention import CrossAttention
from networks import moco
from torchvision.models.resnet import BasicBlock
from torchvision import models

non_linearity = partial(F.relu, inplace=True)


class DBlockMoreDilate(nn.Module):
    def __init__(self, channel):
        super(DBlockMoreDilate, self).__init__()
        self.dilate1 = nn.Conv2d(channel, channel, kernel_size=3, dilation=1, padding=1)
        self.dilate2 = nn.Conv2d(channel, channel, kernel_size=3, dilation=2, padding=2)
        self.dilate3 = nn.Conv2d(channel, channel, kernel_size=3, dilation=4, padding=4)
        self.dilate4 = nn.Conv2d(channel, channel, kernel_size=3, dilation=8, padding=8)
        self.dilate5 = nn.Conv2d(channel, channel, kernel_size=3, dilation=16, padding=16)
        for m in self.modules():
            if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
                if m.bias is not None:
                    m.bias.data.zero_()

    def forward(self, x):
        dilate1_out = non_linearity(self.dilate1(x))
        dilate2_out = non_linearity(self.dilate2(dilate1_out))
        dilate3_out = non_linearity(self.dilate3(dilate2_out))
        dilate4_out = non_linearity(self.dilate4(dilate3_out))
        dilate5_out = non_linearity(self.dilate5(dilate4_out))
        out = x + dilate1_out + dilate2_out + dilate3_out + dilate4_out + dilate5_out
        return out


class DBlock(nn.Module):
    def __init__(self, channel):
        super(DBlock, self).__init__()
        self.dilate1 = nn.Conv2d(channel, channel, kernel_size=3, dilation=1, padding=1)
        self.dilate2 = nn.Conv2d(channel, channel, kernel_size=3, dilation=2, padding=2)
        self.dilate3 = nn.Conv2d(channel, channel, kernel_size=3, dilation=4, padding=4)
        self.dilate4 = nn.Conv2d(channel, channel, kernel_size=3, dilation=8, padding=8)
        # self.dilate5 = nn.Conv2d(channel, channel, kernel_size=3, dilation=16, padding=16)
        for m in self.modules():
            if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
                if m.bias is not None:
                    m.bias.data.zero_()

    def forward(self, x):
        dilate1_out = non_linearity(self.dilate1(x))
        dilate2_out = non_linearity(self.dilate2(dilate1_out))
        dilate3_out = non_linearity(self.dilate3(dilate2_out))
        dilate4_out = non_linearity(self.dilate4(dilate3_out))
        # dilate5_out = non_linearity(self.dilate5(dilate4_out))
        out = x + dilate1_out + dilate2_out + dilate3_out + dilate4_out  # + dilate5_out
        return out


class DecoderBlock(nn.Module):
    def __init__(self, in_channels, n_filters):
        super(DecoderBlock, self).__init__()

        self.conv1 = nn.Conv2d(in_channels, in_channels // 4, 1)
        self.norm1 = nn.BatchNorm2d(in_channels // 4)
        self.relu1 = non_linearity

        self.deconv2 = nn.ConvTranspose2d(in_channels // 4, in_channels // 4, 3, stride=2, padding=1, output_padding=1)
        self.norm2 = nn.BatchNorm2d(in_channels // 4)
        self.relu2 = non_linearity

        self.conv3 = nn.Conv2d(in_channels // 4, n_filters, 1)
        self.norm3 = nn.BatchNorm2d(n_filters)
        self.relu3 = non_linearity

    def forward(self, x):
        x = self.conv1(x)
        x = self.norm1(x)
        x = self.relu1(x)
        x = self.deconv2(x)
        x = self.norm2(x)
        x = self.relu2(x)
        x = self.conv3(x)
        x = self.norm3(x)
        x = self.relu3(x)
        return x


class DLinkNet18(nn.Module):
    def __init__(self, backbone='seco-1m', num_classes=1):
        super(DLinkNet18, self).__init__()

        filters = [64, 128, 256, 512]

        if backbone == 'random':
            resnet = models.resnet18(pretrained=False)
        elif backbone == 'imagenet':
            resnet = models.resnet18(pretrained=True)
        elif backbone == 'seco-100k':
            resnet = moco.resnet18(large=False)
        elif backbone == 'seco-1m':
            resnet = moco.resnet18(large=True)
        else:
            raise ValueError()

        self.first_conv = resnet.conv1
        self.first_bn = resnet.bn1
        self.first_relu = resnet.relu
        self.first_max_pool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3
        self.encoder4 = resnet.layer4

        self.d_block = DBlock(512)

        self.decoder4 = DecoderBlock(filters[3], filters[2])
        self.decoder3 = DecoderBlock(filters[2], filters[1])
        self.decoder2 = DecoderBlock(filters[1], filters[0])
        self.decoder1 = DecoderBlock(filters[0], filters[0])

        self.final_deconv1 = nn.ConvTranspose2d(filters[0], 32, 4, 2, 1)
        self.final_relu1 = non_linearity
        self.final_conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.final_relu2 = non_linearity
        self.final_conv3 = nn.Conv2d(32, num_classes, 3, padding=1)

    def forward(self, x):
        # Encoder
        x = self.first_conv(x)
        x = self.first_bn(x)
        x = self.first_relu(x)
        x = self.first_max_pool(x)
        e1 = self.encoder1(x)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)
        e4 = self.encoder4(e3)

        # Center
        e4 = self.d_block(e4)

        # Decoder
        d4 = self.decoder4(e4) + e3
        d3 = self.decoder3(d4) + e2
        d2 = self.decoder2(d3) + e1
        d1 = self.decoder1(d2)

        out = self.final_deconv1(d1)
        out = self.final_relu1(out)
        out = self.final_conv2(out)
        out = self.final_relu2(out)
        out = self.final_conv3(out)

        return torch.sigmoid(out)


class DLinkNet18HeadsV1(nn.Module):
    def __init__(self, backbone='seco-1m', num_classes=1):
        super(DLinkNet18HeadsV1, self).__init__()

        filters = [64, 128, 256, 512]

        if backbone == 'random':
            resnet = models.resnet18(pretrained=False)
        elif backbone == 'imagenet':
            resnet = models.resnet18(pretrained=True)
        elif backbone == 'seco-100k':
            resnet = moco.resnet18(large=False)
        elif backbone == 'seco-1m':
            resnet = moco.resnet18(large=True)
        else:
            raise ValueError()

        self.first_conv = resnet.conv1
        self.first_bn = resnet.bn1
        self.first_relu = resnet.relu
        self.first_max_pool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3
        self.encoder4 = resnet.layer4
        self.encoder5 = nn.Sequential(
            BasicBlock(512, 512, stride=2, downsample=nn.Sequential(
                nn.Conv2d(512, 512, kernel_size=1, stride=2, bias=False),
                nn.BatchNorm2d(512)
            ),
                       groups=1,
                       base_width=64, dilation=1,
                       norm_layer=nn.BatchNorm2d),
            BasicBlock(512, 512, stride=1, downsample=None,
                       groups=1,
                       base_width=64, dilation=1,
                       norm_layer=nn.BatchNorm2d)
        )

        self.head1 = moco.resnet18_heads(large=True, index=0)
        self.head2 = moco.resnet18_heads(large=True, index=1)
        self.head3 = moco.resnet18_heads(large=True, index=2)

        self.feat_encoder = nn.Sequential(
            nn.Conv2d(filters[3], filters[3], kernel_size=3, stride=2, padding=1),
            nn.BatchNorm2d(filters[3]),
            nn.ReLU(inplace=True)
        )
        self.gate_encoder = nn.Sequential(
            nn.Conv2d(6, filters[3], kernel_size=1, stride=1),
            nn.BatchNorm2d(filters[3]),
            nn.ReLU(inplace=True)
        )
        self.join_encoder = nn.Sequential(
            nn.Conv2d(2 * filters[3], filters[3], kernel_size=1, stride=1),
            nn.BatchNorm2d(filters[3]),
            nn.ReLU(inplace=True)
        )

        self.d_block = DBlock(512)

        self.decoder6 = DecoderBlock(filters[3], filters[3])
        self.decoder5 = DecoderBlock(filters[3], filters[3])
        self.decoder4 = DecoderBlock(filters[3], filters[2])
        self.decoder3 = DecoderBlock(filters[2], filters[1])
        self.decoder2 = DecoderBlock(filters[1], filters[0])
        self.decoder1 = DecoderBlock(filters[0], filters[0])

        self.final_deconv1 = nn.ConvTranspose2d(filters[0], 32, 4, 2, 1)
        self.final_relu1 = non_linearity
        self.final_conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.final_relu2 = non_linearity
        self.final_conv3 = nn.Conv2d(32, num_classes, 3, padding=1)

    def forward(self, x):
        # Encoder
        x = self.first_conv(x)
        x = self.first_bn(x)
        x = self.first_relu(x)
        x = self.first_max_pool(x)
        e1 = self.encoder1(x)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)
        e4 = self.encoder4(e3)
        e5 = self.encoder5(e4)

        h0 = self.head1(e5)
        h1 = self.head2(e5)
        h2 = self.head3(e5)

        h = torch.cat([h0, h1, h2], dim=1)
        h = h.view(-1, 6, 8, 8)

        f = self.feat_encoder(e5)
        h = self.gate_encoder(h)

        g = torch.cat((f, h), 1)
        g = self.join_encoder(g)

        # Center
        e6 = self.d_block(g)

        # Decoder
        d6 = self.decoder6(e6) + e5
        d5 = self.decoder5(d6) + e4
        d4 = self.decoder4(d5) + e3
        d3 = self.decoder3(d4) + e2
        d2 = self.decoder2(d3) + e1
        d1 = self.decoder1(d2)

        out = self.final_deconv1(d1)
        out = self.final_relu1(out)
        out = self.final_conv2(out)
        out = self.final_relu2(out)
        out = self.final_conv3(out)

        return torch.sigmoid(out)


class DLinkNet18HeadsV2(nn.Module):
    def __init__(self, backbone='seco-1m', num_classes=1):
        super(DLinkNet18HeadsV2, self).__init__()

        filters = [64, 128, 256, 512]

        if backbone == 'random':
            resnet = models.resnet18(pretrained=False)
        elif backbone == 'imagenet':
            resnet = models.resnet18(pretrained=True)
        elif backbone == 'seco-100k':
            resnet = moco.resnet18(large=False)
        elif backbone == 'seco-1m':
            resnet = moco.resnet18(large=True)
        else:
            raise ValueError()

        self.first_conv = resnet.conv1
        self.first_bn = resnet.bn1
        self.first_relu = resnet.relu
        self.first_max_pool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3
        self.encoder4 = resnet.layer4

        self.head1 = moco.resnet18_heads(large=True, index=0)
        self.head2 = moco.resnet18_heads(large=True, index=1)
        self.head3 = moco.resnet18_heads(large=True, index=2)

        self.merge_encoder = nn.Sequential(
            nn.Conv2d(3 * filters[1] + filters[3], filters[3], kernel_size=3, stride=1),
            nn.BatchNorm2d(filters[3]),
            nn.ReLU(inplace=True)
        )

        self.d_block = DBlock(512)

        self.decoder4 = DecoderBlock(filters[3], filters[2])
        self.decoder3 = DecoderBlock(filters[2], filters[1])
        self.decoder2 = DecoderBlock(filters[1], filters[0])
        self.decoder1 = DecoderBlock(filters[0], filters[0])

        self.final_deconv1 = nn.ConvTranspose2d(filters[0], 32, 4, 2, 1)
        self.final_relu1 = non_linearity
        self.final_conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.final_relu2 = non_linearity
        self.final_conv3 = nn.Conv2d(32, num_classes, 3, padding=1)

    def forward(self, x):
        # Encoder
        x = self.first_conv(x)
        x = self.first_bn(x)
        x = self.first_relu(x)
        x = self.first_max_pool(x)
        e1 = self.encoder1(x)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)
        e4 = self.encoder4(e3)

        h0 = self.head1(e4)
        h1 = self.head2(e4)
        h2 = self.head3(e4)

        eb, ec, eh, ew = e4.size()

        h = torch.cat([h0, h1, h2], dim=1)
        h = torch.unsqueeze(torch.unsqueeze(h, dim=2), dim=2)
        h = h.repeat(1, 1, eh, ew)

        e4 = torch.cat((h, e4), dim=1)
        e4 = self.merge_encoder(e4)

        # Center
        e4 = self.d_block(e4)

        # Decoder
        d4 = self.decoder4(e4) + e3
        d3 = self.decoder3(d4) + e2
        d2 = self.decoder2(d3) + e1
        d1 = self.decoder1(d2)

        out = self.final_deconv1(d1)
        out = self.final_relu1(out)
        out = self.final_conv2(out)
        out = self.final_relu2(out)
        out = self.final_conv3(out)

        return torch.sigmoid(out)


class DLinkNet18HeadsV3(nn.Module):
    def __init__(self, backbone='seco-1m', num_classes=1):
        super(DLinkNet18HeadsV3, self).__init__()

        filters = [64, 128, 256, 512]

        if backbone == 'random':
            resnet = models.resnet18(pretrained=False)
        elif backbone == 'imagenet':
            resnet = models.resnet18(pretrained=True)
        elif backbone == 'seco-100k':
            resnet = moco.resnet18(large=False)
        elif backbone == 'seco-1m':
            resnet = moco.resnet18(large=True)
        else:
            raise ValueError()

        self.first_conv = resnet.conv1
        self.first_bn = resnet.bn1
        self.first_relu = resnet.relu
        self.first_max_pool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3
        self.encoder4 = resnet.layer4

        # self.head1 = moco.resnet18_heads(large=True, index=0, decode=True)
        self.head2 = moco.resnet18_heads(large=True, index=1, decode=True)
        # self.head3 = moco.resnet18_heads(large=True, index=2, decode=True)

        self.a_block = CBAMBlock(channel=filters[3], reduction=filters[3] // filters[0], kernel_size=3)

        self.d_block = DBlock(512)

        self.decoder4 = DecoderBlock(filters[3], filters[2])
        self.decoder3 = DecoderBlock(filters[2], filters[1])
        self.decoder2 = DecoderBlock(filters[1], filters[0])
        self.decoder1 = DecoderBlock(filters[0], filters[0])

        self.final_deconv1 = nn.ConvTranspose2d(filters[0], 32, 4, 2, 1)
        self.final_relu1 = non_linearity
        self.final_conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.final_relu2 = non_linearity
        self.final_conv3 = nn.Conv2d(32, num_classes, 3, padding=1)

    def forward(self, x):
        # Encoder
        x = self.first_conv(x)
        x = self.first_bn(x)
        x = self.first_relu(x)
        x = self.first_max_pool(x)
        e1 = self.encoder1(x)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)
        e4 = self.encoder4(e3)

        # h0 = self.head1(e4)
        h1 = self.head2(e4)
        # h2 = self.head3(e4)

        # h0 = torch.unsqueeze(torch.unsqueeze(h0, 2), 3)
        h1 = torch.unsqueeze(torch.unsqueeze(h1, 2), 3)
        # h2 = torch.unsqueeze(torch.unsqueeze(h2, 2), 3)

        # Center
        e4 = e4 + e4 * h1
        e4 = self.d_block(e4)

        # Decoder
        d4 = self.decoder4(e4) + e3
        d3 = self.decoder3(d4) + e2
        d2 = self.decoder2(d3) + e1
        d1 = self.decoder1(d2)

        out = self.final_deconv1(d1)
        out = self.final_relu1(out)
        out = self.final_conv2(out)
        out = self.final_relu2(out)
        out = self.final_conv3(out)

        return torch.sigmoid(out)


class CBAMDLinkNet18(nn.Module):
    def __init__(self, backbone='seco-1m', num_classes=1):
        super(CBAMDLinkNet18, self).__init__()

        filters = [64, 128, 256, 512]

        if backbone == 'random':
            resnet = models.resnet18(pretrained=False)
        elif backbone == 'imagenet':
            resnet = models.resnet18(pretrained=True)
        elif backbone == 'seco-100k':
            resnet = moco.resnet18(large=False)
        elif backbone == 'seco-1m':
            resnet = moco.resnet18(large=True)
        else:
            raise ValueError()

        self.first_conv = resnet.conv1
        self.first_bn = resnet.bn1
        self.first_relu = resnet.relu
        self.first_max_pool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3
        self.encoder4 = resnet.layer4

        self.a_block = CBAMBlock(channel=filters[3], reduction=filters[3] // filters[0], kernel_size=3)

        self.d_block = DBlock(512)

        self.decoder4 = DecoderBlock(filters[3], filters[2])
        self.decoder3 = DecoderBlock(filters[2], filters[1])
        self.decoder2 = DecoderBlock(filters[1], filters[0])
        self.decoder1 = DecoderBlock(filters[0], filters[0])

        self.final_deconv1 = nn.ConvTranspose2d(filters[0], 32, 4, 2, 1)
        self.final_relu1 = non_linearity
        self.final_conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.final_relu2 = non_linearity
        self.final_conv3 = nn.Conv2d(32, num_classes, 3, padding=1)

    def forward(self, x):
        # Encoder
        x = self.first_conv(x)
        x = self.first_bn(x)
        x = self.first_relu(x)
        x = self.first_max_pool(x)
        e1 = self.encoder1(x)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)
        e4 = self.encoder4(e3)

        # Center
        e4 = self.a_block(e4)
        e4 = self.d_block(e4)

        # Decoder
        d4 = self.decoder4(e4) + e3
        d3 = self.decoder3(d4) + e2
        d2 = self.decoder2(d3) + e1
        d1 = self.decoder1(d2)

        out = self.final_deconv1(d1)
        out = self.final_relu1(out)
        out = self.final_conv2(out)
        out = self.final_relu2(out)
        out = self.final_conv3(out)

        return torch.sigmoid(out)


class DLinkNet34LessPool(nn.Module):
    def __init__(self, num_classes=1):
        super(DLinkNet34LessPool, self).__init__()

        filters = [64, 128, 256, 512]
        resnet = models.resnet34(pretrained=True)

        self.first_conv = resnet.conv1
        self.first_bn = resnet.bn1
        self.first_relu = resnet.relu
        self.first_max_pool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3

        self.d_block = DBlockMoreDilate(256)

        self.decoder3 = DecoderBlock(filters[2], filters[1])
        self.decoder2 = DecoderBlock(filters[1], filters[0])
        self.decoder1 = DecoderBlock(filters[0], filters[0])

        self.final_deconv1 = nn.ConvTranspose2d(filters[0], 32, 4, 2, 1)
        self.final_relu1 = non_linearity
        self.final_conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.final_relu2 = non_linearity
        self.final_conv3 = nn.Conv2d(32, num_classes, 3, padding=1)

    def forward(self, x):
        # Encoder
        x = self.first_conv(x)
        x = self.first_bn(x)
        x = self.first_relu(x)
        x = self.first_max_pool(x)
        e1 = self.encoder1(x)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)

        # Center
        e3 = self.d_block(e3)

        # Decoder
        d3 = self.decoder3(e3) + e2
        d2 = self.decoder2(d3) + e1
        d1 = self.decoder1(d2)

        # Final Classification
        out = self.final_deconv1(d1)
        out = self.final_relu1(out)
        out = self.final_conv2(out)
        out = self.final_relu2(out)
        out = self.final_conv3(out)

        return torch.sigmoid(out)


class DLinkNet34(nn.Module):
    def __init__(self, backbone='imagenet', num_classes=1, num_channels=3, interp_mode=None):
        super(DLinkNet34, self).__init__()

        filters = [64, 128, 256, 512]

        if backbone == 'random':
            resnet = models.resnet34(pretrained=False)
        elif backbone == 'imagenet':
            resnet = models.resnet34(pretrained=True)
        elif backbone == 'seco-1m':
            resnet = moco.resnet34()
        else:
            raise ValueError()

        if interp_mode is None:
            self.up_sample = nn.Identity()
        else:
            self.up_sample = nn.Upsample(size=(1024, 1024), mode=interp_mode)

        self.first_conv = resnet.conv1
        self.first_bn = resnet.bn1
        self.first_relu = resnet.relu
        self.first_max_pool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3
        self.encoder4 = resnet.layer4

        self.d_block = DBlock(512)

        # self.d_block4 = DBlock(512)
        # self.d_block3 = DBlock(256)
        # self.d_block2 = DBlock(128)
        # self.d_block1 = DBlock(64)

        self.decoder4 = DecoderBlock(filters[3], filters[2])
        self.decoder3 = DecoderBlock(filters[2], filters[1])
        self.decoder2 = DecoderBlock(filters[1], filters[0])
        self.decoder1 = DecoderBlock(filters[0], filters[0])

        self.final_deconv1 = nn.ConvTranspose2d(filters[0], 32, 4, 2, 1)
        self.final_relu1 = non_linearity
        self.final_conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.final_relu2 = non_linearity
        self.final_conv3 = nn.Conv2d(32, num_classes, 3, padding=1)

    def forward(self, x):
        # Encoder
        x = self.up_sample(x)
        x = self.first_conv(x)
        x = self.first_bn(x)
        x = self.first_relu(x)
        x = self.first_max_pool(x)
        e1 = self.encoder1(x)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)
        e4 = self.encoder4(e3)

        # Center
        e4 = self.d_block(e4)

        # e4 = self.d_block4(e4)
        # e3 = self.d_block3(e3)
        # e2 = self.d_block2(e2)
        # e1 = self.d_block1(e1)

        # Decoder
        d4 = self.decoder4(e4) + e3
        d3 = self.decoder3(d4) + e2
        d2 = self.decoder2(d3) + e1
        d1 = self.decoder1(d2)

        out = self.final_deconv1(d1)
        out = self.final_relu1(out)
        out = self.final_conv2(out)
        out = self.final_relu2(out)
        out = self.final_conv3(out)

        return torch.sigmoid(out)


class DLinkNet50(nn.Module):
    def __init__(self, backbone='imagenet', num_classes=1):
        super(DLinkNet50, self).__init__()

        filters = [256, 512, 1024, 2048]

        if backbone == 'random':
            resnet = models.resnet50(pretrained=False)
        elif backbone == 'imagenet':
            resnet = models.resnet50(pretrained=True)
        elif backbone == 'seco-100k':
            resnet = moco.resnet50(large=False)
        elif backbone == 'seco-1m':
            resnet = moco.resnet50(large=True)
        else:
            raise ValueError()

        self.first_conv = resnet.conv1
        self.first_bn = resnet.bn1
        self.first_relu = resnet.relu
        self.first_max_pool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3
        self.encoder4 = resnet.layer4

        self.d_block = DBlockMoreDilate(2048)

        self.decoder4 = DecoderBlock(filters[3], filters[2])
        self.decoder3 = DecoderBlock(filters[2], filters[1])
        self.decoder2 = DecoderBlock(filters[1], filters[0])
        self.decoder1 = DecoderBlock(filters[0], filters[0])

        self.final_deconv1 = nn.ConvTranspose2d(filters[0], 32, 4, 2, 1)
        self.final_relu1 = non_linearity
        self.final_conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.final_relu2 = non_linearity
        self.final_conv3 = nn.Conv2d(32, num_classes, 3, padding=1)

    def forward(self, x):
        # Encoder
        x = self.first_conv(x)
        x = self.first_bn(x)
        x = self.first_relu(x)
        x = self.first_max_pool(x)
        e1 = self.encoder1(x)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)
        e4 = self.encoder4(e3)

        # Center
        e4 = self.d_block(e4)

        # Decoder
        d4 = self.decoder4(e4) + e3
        d3 = self.decoder3(d4) + e2
        d2 = self.decoder2(d3) + e1
        d1 = self.decoder1(d2)

        out = self.final_deconv1(d1)
        out = self.final_relu1(out)
        out = self.final_conv2(out)
        out = self.final_relu2(out)
        out = self.final_conv3(out)

        return torch.sigmoid(out)


class DLinkNet101(nn.Module):
    def __init__(self, num_classes=1):
        super(DLinkNet101, self).__init__()

        filters = [256, 512, 1024, 2048]
        resnet = models.resnet101(pretrained=True)
        self.first_conv = resnet.conv1
        self.first_bn = resnet.bn1
        self.first_relu = resnet.relu
        self.first_max_pool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3
        self.encoder4 = resnet.layer4

        self.d_block = DBlockMoreDilate(2048)

        self.decoder4 = DecoderBlock(filters[3], filters[2])
        self.decoder3 = DecoderBlock(filters[2], filters[1])
        self.decoder2 = DecoderBlock(filters[1], filters[0])
        self.decoder1 = DecoderBlock(filters[0], filters[0])

        self.final_deconv1 = nn.ConvTranspose2d(filters[0], 32, 4, 2, 1)
        self.final_relu1 = non_linearity
        self.final_conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.final_relu2 = non_linearity
        self.final_conv3 = nn.Conv2d(32, num_classes, 3, padding=1)

    def forward(self, x):
        # Encoder
        x = self.first_conv(x)
        x = self.first_bn(x)
        x = self.first_relu(x)
        x = self.first_max_pool(x)
        e1 = self.encoder1(x)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)
        e4 = self.encoder4(e3)

        # Center
        e4 = self.d_block(e4)

        # Decoder
        d4 = self.decoder4(e4) + e3
        d3 = self.decoder3(d4) + e2
        d2 = self.decoder2(d3) + e1
        d1 = self.decoder1(d2)
        out = self.final_deconv1(d1)
        out = self.final_relu1(out)
        out = self.final_conv2(out)
        out = self.final_relu2(out)
        out = self.final_conv3(out)

        return torch.sigmoid(out)


class LinkNet34(nn.Module):
    def __init__(self, num_classes=1):
        super(LinkNet34, self).__init__()

        filters = [64, 128, 256, 512]
        resnet = models.resnet34(pretrained=True)
        self.first_conv = resnet.conv1
        self.first_bn = resnet.bn1
        self.first_relu = resnet.relu
        self.first_max_pool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3
        self.encoder4 = resnet.layer4

        self.decoder4 = DecoderBlock(filters[3], filters[2])
        self.decoder3 = DecoderBlock(filters[2], filters[1])
        self.decoder2 = DecoderBlock(filters[1], filters[0])
        self.decoder1 = DecoderBlock(filters[0], filters[0])

        self.final_deconv1 = nn.ConvTranspose2d(filters[0], 32, 3, stride=2)
        self.final_relu1 = non_linearity
        self.final_conv2 = nn.Conv2d(32, 32, 3)
        self.final_relu2 = non_linearity
        self.final_conv3 = nn.Conv2d(32, num_classes, 2, padding=1)

    def forward(self, x):
        # Encoder
        x = self.first_conv(x)
        x = self.first_bn(x)
        x = self.first_relu(x)
        x = self.first_max_pool(x)
        e1 = self.encoder1(x)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)
        e4 = self.encoder4(e3)

        # Decoder
        d4 = self.decoder4(e4) + e3
        d3 = self.decoder3(d4) + e2
        d2 = self.decoder2(d3) + e1
        d1 = self.decoder1(d2)
        out = self.final_deconv1(d1)
        out = self.final_relu1(out)
        out = self.final_conv2(out)
        out = self.final_relu2(out)
        out = self.final_conv3(out)

        return torch.sigmoid(out)
