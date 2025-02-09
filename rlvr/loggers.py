import ray
from collections import defaultdict
from torch.utils.tensorboard import SummaryWriter


class BaseLogger:

    def __init__(self):
        self._metrics = defaultdict(list)

    def store(self, key, value):
        self._metrics[key].append(value)

    def store_list(self, key, values):
        self._metrics[key].extend(list(values))


@ray.remote
class NoLogger(BaseLogger):

    def __init__(self):
        super().__init__()

    def log(self, step):
        return True


@ray.remote
class StdoutLogger(BaseLogger):

    def __init__(self):
        super().__init__()

    def log(self, step):
        for key, value_list in self._metrics.items():
            mean_val = sum(value_list) / len(value_list)
            print(f"step={step}, {key}={mean_val}")
        return True


@ray.remote
class TensorboardLogger(BaseLogger):

    def __init__(self, log_dir):
        super().__init__()
        self._writer = SummaryWriter(log_dir)

    def log(self, step):
        for key, value_list in self._metrics.items():
            mean_val = sum(value_list) / len(value_list)
            max_val = max(value_list)
            min_val = min(value_list)
            self._writer.add_scalar(f"{key}/mean", mean_val, step)
            self._writer.add_scalar(f"{key}/max", max_val, step)
            self._writer.add_scalar(f"{key}/min", min_val, step)
        return True
