import timm
from utils.args import *
from models.utils.continual_model import ContinualModel
import torch
from datasets import get_dataset

def get_parser() -> ArgumentParser:
    parser = ArgumentParser(description='Continual Learning via'
                                        ' Progressive Neural Networks.')
    add_management_args(parser)
    add_experiment_args(parser)
    # add_aux_dataset_args(parser)
    parser.add_argument('--network', type=str, default='vit_base_patch16_224', help='Network to use')
    parser.add_argument('--pretrained', type=int, choices=[0, 1], default=1, help='Should use pretrained weights?')
    return parser


class SgdAce(ContinualModel):
    NAME = 'sgd_ace'
    COMPATIBILITY = ['class-il', 'domain-il', 'task-il', 'general-continual']

    def get_backbone(self, args):
        pretrained = args.pretrained == 1
        backbone = timm.create_model(args.network, pretrained=pretrained, num_classes=self.n_classes)
        return backbone

    def __init__(self, backbone, loss, args, transform):
        self.dataset = get_dataset(args)
        self.n_classes = self.dataset.N_CLASSES if hasattr(self.dataset, 'N_CLASSES') else self.dataset.N_CLASSES_PER_TASK * self.dataset.N_TASKS
        self.pretrained = args.pretrained == 1
        backbone = self.get_backbone(args)
        super().__init__(backbone, loss, args, transform)
        self.current_task = 0

    def end_task(self, dataset):
        if self.args.save_checkpoints:
            self.savecheck_martin()
        self.current_task += 1


    def observe(self, inputs, labels, not_aug_inputs, epoch=0):
        labels = labels.long()
        self.opt.zero_grad()
        outputs = self.net(inputs)
        offset_1, offset_2 = self._compute_offsets(self.current_task)
        idx = labels.sum(0).nonzero().squeeze(1)
        filtered_outputs = outputs[:, idx]
        filtered_labels = labels[:, idx]
        loss = self.args.loss_w * self.loss(filtered_outputs, filtered_labels.float())
        loss.backward()
        if self.args.clip_grad is not None:
            torch.nn.utils.clip_grad_norm_(self.net.parameters(), self.args.clip_grad)
        self.opt.step()

        return loss.item()
    
    def forward(self, x):
        offset_1, offset_2 = self._compute_offsets(self.current_task-1)
        return self.net(x)[:, :offset_2]