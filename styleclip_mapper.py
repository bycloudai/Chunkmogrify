import math
import torch
from torch import nn
from torch.nn import functional as F
from torch.nn import Module

def fused_leaky_relu(input, bias, negative_slope=0.2, scale=2 ** 0.5):
    rest_dim = [1] * (input.ndim - bias.ndim - 1)
    input = input.cuda()
    if input.ndim == 3:
        return (
            F.leaky_relu(
                input + bias.view(1, *rest_dim, bias.shape[0]), negative_slope=negative_slope
            )
            * scale
        )
    else:
        return (
            F.leaky_relu(
                input + bias.view(1, bias.shape[0], *rest_dim), negative_slope=negative_slope
            )
            * scale
        )

class PixelNorm(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, input):
        return input * torch.rsqrt(torch.mean(input ** 2, dim=1, keepdim=True) + 1e-8)


class EqualLinear(nn.Module):
    def __init__(
        self, in_dim, out_dim, bias=True, bias_init=0, lr_mul=1, activation=None
    ):
        super().__init__()

        self.weight = nn.Parameter(torch.randn(out_dim, in_dim).div_(lr_mul))

        if bias:
            self.bias = nn.Parameter(torch.zeros(out_dim).fill_(bias_init))

        else:
            self.bias = None

        self.activation = activation

        self.scale = (1 / math.sqrt(in_dim)) * lr_mul
        self.lr_mul = lr_mul

    def forward(self, input):
        if self.activation:
            out = F.linear(input, self.weight * self.scale)
            out = fused_leaky_relu(out, self.bias * self.lr_mul)

        else:
            out = F.linear(
                input, self.weight * self.scale, bias=self.bias * self.lr_mul
            )

        return out

    def __repr__(self):
        return (
            f'{self.__class__.__name__}({self.weight.shape[1]}, {self.weight.shape[0]})'
        )

class Mapper(Module):

    def __init__(self, opts):
        super(Mapper, self).__init__()

        self.opts = opts
        layers = [PixelNorm()]

        for i in range(4):
            layers.append(
                EqualLinear(
                    512, 512, lr_mul=0.01, activation='fused_lrelu'
                )
            )

        self.mapping = nn.Sequential(*layers)


    def forward(self, x):
        x = self.mapping(x)
        return x


class SingleMapper(Module):

    def __init__(self, opts):
        super(SingleMapper, self).__init__()

        self.opts = opts

        self.mapping = Mapper(opts)

    def forward(self, x):
        out = self.mapping(x)
        return out


class LevelsMapper(Module):

    def __init__(self, opts):
        super(LevelsMapper, self).__init__()

        self.opts = opts

        if not opts.no_coarse_mapper:
            self.course_mapping = Mapper(opts)
        if not opts.no_medium_mapper:
            self.medium_mapping = Mapper(opts)
        if not opts.no_fine_mapper:
            self.fine_mapping = Mapper(opts)

    def forward(self, x):
        x_coarse = x[:, :4, :]
        x_medium = x[:, 4:8, :]
        x_fine = x[:, 8:, :]

        if not self.opts.no_coarse_mapper:
            x_coarse = self.course_mapping(x_coarse)
        else:
            x_coarse = torch.zeros_like(x_coarse)
        if not self.opts.no_medium_mapper:
            x_medium = self.medium_mapping(x_medium)
        else:
            x_medium = torch.zeros_like(x_medium)
        if not self.opts.no_fine_mapper:
            x_fine = self.fine_mapping(x_fine)
        else:
            x_fine = torch.zeros_like(x_fine)


        out = torch.cat([x_coarse, x_medium, x_fine], dim=1)

        return out

def get_keys(d, name):
    if 'state_dict' in d:
        d = d['state_dict']
    d_filt = {k[len(name) + 1:]: v for k, v in d.items() if k[:len(name)] == name}
    return d_filt


class StyleCLIPMapper(nn.Module):

    def __init__(self, opts):
        super().__init__()
        self.opts = opts
        # Define architecture
        self.mapper = self.set_mapper()
        # Load weights if needed
        self.load_weights()

    def set_mapper(self):
        if self.opts.mapper_type == 'SingleMapper':
            mapper = SingleMapper(self.opts)
        elif self.opts.mapper_type == 'LevelsMapper':
            mapper = LevelsMapper(self.opts)
        else:
            raise Exception('{} is not a valid mapper'.format(self.opts.mapper_type))
        return mapper

    def load_weights(self):
        if self.opts.checkpoint_path is not None:
            print('Loading from checkpoint: {}'.format(self.opts.checkpoint_path))
            ckpt = torch.load(self.opts.checkpoint_path, map_location='cpu')
            self.mapper.load_state_dict(get_keys(ckpt, 'mapper'), strict=True)
