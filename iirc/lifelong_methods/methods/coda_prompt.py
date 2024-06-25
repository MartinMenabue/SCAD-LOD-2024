import torch.nn as nn
import torch.distributed as dist
import torch
import numpy as np
from PIL import Image
import warnings
from typing import Optional, Union, List, Dict, Callable, Tuple

from iirc.lifelong_dataset.torch_dataset import Dataset
from iirc.definitions import NO_LABEL_PLACEHOLDER
from lifelong_methods.buffer.buffer import BufferBase
from lifelong_methods.methods.base_method import BaseMethod

from lifelong_methods.methods.coda_prompt_utils.models.zoo_ours import ResNetZoo
from lifelong_methods.methods.coda_prompt_utils.models.schedulers import CosineSchedule
import wandb

class Model(BaseMethod):
    """
    A finetuning (Experience Replay) baseline.
    """

    def __init__(self, n_cla_per_tsk: Union[np.ndarray, List[int]], class_names_to_idx: Dict[str, int], config: Dict):
        super(Model, self).__init__(n_cla_per_tsk, class_names_to_idx, config)

        self.net = ResNetZoo(num_classes=self.num_classes, pt=True, mode=0, prompt_flag='dual', prompt_param=[len(self.n_cla_per_tsk), [self.config['pool_size_coda'], 8, 0, 0, 0]])
        self.current_task = 0
        self.net.task_id = 0


        # setup losses
        self.bce = nn.BCEWithLogitsLoss(reduction="mean")
        self.opt = self.get_optimizer()

    def _load_method_state_dict(self, state_dicts: Dict[str, Dict]) -> None:
        """
        This is where anything model specific needs to be done before the state_dicts are loaded

        Args:
            state_dicts (Dict[str, Dict]): a dictionary with the state dictionaries of this method, the optimizer, the
            scheduler, and the values of the variables whose names are inside the self.method_variables
        """
        pass

    def get_optimizer(self):
        params_to_opt = list(self.net.prompt.parameters()) + list(self.net.last.parameters())
        optimizer_arg = {'params':params_to_opt,
                        'lr':self.lr,
                        'weight_decay': self.weight_decay}
        if self.optimizer_type == 'sgd':
            opt = torch.optim.SGD(**optimizer_arg)
        elif self.optimizer_type == 'adam':
            opt = torch.optim.Adam(**optimizer_arg)
        else:
            raise ValueError('Optimizer not supported for this method')
        return opt

    def _prepare_model_for_new_task(self, **kwargs) -> None:
        if self.current_task != 0:
            self.net.task_id = self.current_task
            self.net.prompt.process_frequency()
        self.opt = self.get_optimizer()
        
        if self.current_task == 0:
            self.scheduler = CosineSchedule(self.opt, K=self.epochs_per_task*2)
        else:
            self.scheduler = CosineSchedule(self.opt, K=self.epochs_per_task)
        self.old_epoch = 0
        """
        A method specific function that takes place before the starting epoch of each new task (runs from the
            prepare_model_for_task function)
        """
        pass

    def observe(self, x: torch.Tensor, y: torch.Tensor, in_buffer: Optional[torch.Tensor] = None,
                train: bool = True, epoch=0, not_aug_inputs=None) -> Tuple[torch.Tensor, float]:
        """
        The method used for training and validation, returns a tensor of model predictions and the loss
        This function needs to be defined in the inheriting method class

        Args:
            x (torch.Tensor): The batch of images
            y (torch.Tensor): A 2-d batch indicator tensor of shape (number of samples x number of classes)
            in_buffer (Optional[torch.Tensor]): A 1-d boolean tensor which indicates which sample is from the buffer.
            train (bool): Whether this is training or validation/test

        Returns:
            Tuple[torch.Tensor, float]:
            predictions (torch.Tensor) : a 2-d float tensor of the model predictions of shape (number of samples x number of classes)
            loss (float): the value of the loss
        """
        if train and self.old_epoch != epoch:
            self.scheduler.step()
            self.old_epoch = epoch
        
        num_seen_classes = len(self.seen_classes)
        offset_1, offset_2 = self._compute_offsets(self.cur_task_id)
        target = y
        assert target.shape[1] == offset_2
        logits, loss_prompt = self.net(x, train=train)
        loss_prompt = loss_prompt.sum()
        output = logits[:, :offset_2]
        idx = target.sum(0).nonzero().squeeze(1)
        filtered_output = output[:, idx]
        filtered_target = target[:, idx]
        loss = self.bce(filtered_output / self.temperature, filtered_target)

        # TODO weigh the buffer loss by the self.memory_strength before getting the loss mean (use in_buffer)
        if train:
            self.opt.zero_grad()
            loss.backward()
            self.opt.step()

            if self.config['wandb_log']:
                wandb.log({"loss": loss.item()})

        predictions = output > 0.0
        return predictions, loss.item()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        The method used during inference, returns a tensor of model predictions

        Args:
            x (torch.Tensor): The batch of images

        Returns:
            torch.Tensor: a 2-d float tensor of the model predictions of shape (number of samples x number of classes)
        """
        num_seen_classes = len(self.seen_classes)
        output, _ = self.forward_net(x)
        output = output[:, :num_seen_classes]
        predictions = output > 0.0
        return predictions

    def _consolidate_epoch_knowledge(self, **kwargs) -> None:
        """
        A method specific function that takes place after training on each epoch (runs from the
        consolidate_epoch_knowledge function)
        """
        pass

    def consolidate_task_knowledge(self, **kwargs) -> None:
        """Takes place after training on each task"""
        if self.config['save_checkpoints'] and self.current_task in [0, 1, 2, self.num_tasks - 1]:
            self.save_checkpoints()
        self.current_task += 1
        pass


class Buffer(BufferBase):
    def __init__(self,
                 config: Dict,
                 buffer_dir: Optional[str] = None,
                 map_size: int = 1e9,
                 essential_transforms_fn: Optional[Callable[[Image.Image], torch.Tensor]] = None,
                 augmentation_transforms_fn: Optional[Callable[[Image.Image], torch.Tensor]] = None):
        super(Buffer, self).__init__(config, buffer_dir, map_size, essential_transforms_fn, augmentation_transforms_fn)

    def _reduce_exemplar_set(self, **kwargs) -> None:
        """remove extra exemplars from the buffer"""
        for label in self.seen_classes:
            if len(self.mem_class_x[label]) > self.n_mems_per_cla:
                n = len(self.mem_class_x[label]) - self.n_mems_per_cla
                self.remove_samples(label, n)

    def _construct_exemplar_set(self, task_data: Dataset, dist_args: Optional[Dict] = None, **kwargs) -> None:
        """
        update the buffer with the new task exemplars, chosen randomly for each class.

        Args:
            new_task_data (Dataset): The new task data
            dist_args (Optional[Dict]): a dictionary of the distributed processing values in case of multiple gpu (ex:
            rank of the device) (default: None)
        """
        distributed = dist_args is not None
        if distributed:
            rank = dist_args['rank']
        else:
            rank = 0
        new_class_labels = task_data.cur_task

        for class_label in new_class_labels:
            num_images_to_add = min(self.n_mems_per_cla, self.max_mems_pool_size)
            class_images_indices = task_data.get_image_indices_by_cla(class_label, num_images_to_add)
            if distributed:
                device = torch.device(f"cuda:{dist_args['gpu']}")
                class_images_indices_to_broadcast = torch.from_numpy(class_images_indices).to(device)
                torch.distributed.broadcast(class_images_indices_to_broadcast, 0)
                class_images_indices = class_images_indices_to_broadcast.cpu().numpy()

            for image_index in class_images_indices:
                image, label1, label2 = task_data.get_item(image_index)
                if label2 != NO_LABEL_PLACEHOLDER:
                    warnings.warn(f"Sample is being added to the buffer with labels {label1} and {label2}")
                self.add_sample(class_label, image, (label1, label2), rank=rank)