import numpy as np
import torch
from torch.utils.data import Dataset
from torch.autograd import Variable
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


class VehicleRoutingDataset(Dataset):

    def __init__(self, num_samples, input_size, max_load=20, max_demand=9, seed=1234):
        super(VehicleRoutingDataset, self).__init__()

        assert max_load > max_demand
        np.random.seed(seed)
        torch.manual_seed(seed)

        self.num_samples = num_samples
        self.input_size = input_size
        self.max_load = max_load
        self.max_demand = max_demand

        # Driver location will be the first node in each
        locations = np.random.uniform(0, 1, (num_samples, 2, input_size + 1))
        self.static = torch.FloatTensor(locations)

        # Vehicle needs a load > 0 which gets broadcasted to all states
        loads = np.full((num_samples, 1, input_size + 1), max_load) / float(max_load)

        # All nodes are assigned a random demand in [1, max_demand]
        demands = np.random.randint(1, max_demand + 1, (num_samples, 1, input_size + 1))
        demands = demands / float(max_load)

        # The depot will be used to refill the vehicle with the initial load
        demands[:, 0, 0] = 0
        self.dynamic = torch.FloatTensor(np.concatenate((loads, demands), axis=1))

    def __len__(self):
        return self.num_samples

    def __getitem__(self, idx):
        # (static, dynamic, start_loc)
        return (self.static[idx], self.dynamic[idx], self.static[idx, :, 0])

    def update_mask(self, mask, dynamic, chosen_idx=None):
        """Updates the mask used to hide non-valid states.

        Note that all math is done using integers to avoid float errors

        Parameters
        ----------
        dynamic: torch.autograd.Variable of size (1, num_feats, seq_len)
        """

        if dynamic.is_cuda:
            home = torch.cuda.FloatTensor(1, mask.size(1)).fill_(0)
        else:
            home = torch.FloatTensor(1, mask.size(1)).fill_(0)
        home[0, 0] = 1

        dynamic_int = (dynamic.data * self.max_load).int()

        # Nodes with 0-demand cannot be chosen
        demand_mask = dynamic_int[:, 1].ne(0).float()

        # If there's no demand left, terminate search
        if not demand_mask[:, 1:].byte().any():
            return demand_mask * 0.

        has_no_load = dynamic_int[:, 0, 0].eq(0).float()
        has_no_demand = demand_mask[:, 1:].sum(1).eq(0).float()

        # If a vehicle has no load or demand, force it to stay at the depot
        combined = (has_no_load + has_no_demand).gt(0)
        if combined.byte().any():
            idx = combined.nonzero().squeeze()
            demand_mask[idx] = home.expand(int(combined.sum()), -1)

        # Don't let the vehicle visit the depot back-to-back
        has_any_demand = dynamic_int[:, 1, 1:].gt(0).sum(1).float()
        has_full_load = dynamic_int[:, 0, 0].eq(self.max_load).float()

        # Don't let it go home if we have a full load and there is demand remaining
        combined = (has_full_load * has_any_demand).gt(0)
        if combined.byte().any():
            idx = combined.nonzero().squeeze()
            demand_mask[idx] *= (1 - home).expand(int(combined.sum()), -1)

        return demand_mask

    def update_dynamic(self, dynamic_var, chosen_idx):

        # Clone the dynamic variable so we don't mess up graph
        tensor = dynamic_var.data.clone()
        all_loads = tensor[:, 0]
        all_demands = tensor[:, 1]

        # Visit a node other then the depot; Do operations on all elements, and
        # only update those we actually visited at the end
        visit = chosen_idx.ne(0)
        depot = chosen_idx.eq(0)

        load = torch.gather(all_loads, 1, chosen_idx.unsqueeze(1))
        demand = torch.gather(all_demands, 1, chosen_idx.unsqueeze(1))

        if visit.any():

            # Calculate the change in load / demand across ALL of the chosen
            # nodes this round;
            load_int = (load * self.max_load).int().float()
            demand_int = (demand * self.max_load).int().float()

            load_t = torch.clamp(load_int - demand_int, min=0) / self.max_load
            load_t = load_t.expand(-1, tensor.size(2))

            demand_ = torch.clamp(demand_int - load_int, min=0) / self.max_load
            demand_t = demand.masked_scatter_(visit.unsqueeze(1), demand_.squeeze(1))

            visit_idx = visit.nonzero().squeeze()
            all_loads[visit_idx] = load_t[visit_idx]  # Broadcast load update
            all_demands.scatter_(1, chosen_idx.unsqueeze(1), demand_t)  # Update idx

            # Update the amount of material we could pick up by visiting the depot
            visit = visit.float()
            all_demands[:, 0] = all_demands[:, 0] * (1 - visit) + (load_t[:, 0] - 1) * visit

        # Return to depot to fill vehicle load
        if depot.any():
            all_loads[depot.nonzero().squeeze()] = 1.

            # If we visit the depot, we refill the vehicle and have 0 demand to
            # immediately visit it again
            all_demands[:, 0] = all_demands[:, 0] * (1. - depot.float())

        tensor = torch.cat((all_loads.unsqueeze(1), all_demands.unsqueeze(1)), 1)
        return Variable(tensor)


def reward(static, tour_indices, use_cuda=False):
    """
    Euclidean distance between all cities / nodes given by tour_indices
    """

    # Convert the indices back into a tour
    idx = tour_indices.unsqueeze(1).expand(-1, static.size(1), -1)

    tour = torch.gather(static.data, 2, idx).permute(0, 2, 1)

    start = static.data[:, :, 0].unsqueeze(1)

    # Ensure we're always returning to the depot - not the extra concat
    # won't add any extra loss, as the euclidean distance between consecutive
    # points is 0
    y = torch.cat((start, tour, start), dim=1)

    # Euclidean distance between each consecutive point
    tour_len = torch.sqrt(torch.sum(torch.pow(y[:, :-1] - y[:, 1:], 2), dim=2))

    return Variable(tour_len).sum(1)


def render(static, tour_indices, save_path):

    plt.close('all')

    num_plots = min(int(np.sqrt(len(tour_indices))), 3)
    _, axes = plt.subplots(nrows=num_plots, ncols=num_plots,
                           sharex='col', sharey='row')
    axes = [a for ax in axes for a in ax]

    for i, ax in enumerate(axes):

        # Convert the indices back into a tour
        idx = tour_indices[i]
        if len(idx.size()) == 1:
            idx = idx.unsqueeze(0)

        idx = idx.expand(static.size(1), -1)
        data = torch.gather(static[i].data, 1, idx).cpu().numpy()

        start = static[i, :, 0].cpu().data.numpy()
        x = np.hstack((start[0], data[0], start[0]))
        y = np.hstack((start[1], data[1], start[1]))

        # Assign each subtour a different colour & label in order traveled
        idx = np.hstack((0, tour_indices[i].cpu().numpy().flatten(), 0))
        where = np.where(idx == 0)[0]
        count = 0

        for j in range(len(where) - 1):

            count += 1
            low = where[j]
            high = where[j + 1]

            if low + 1 == high:
                continue

            ax.plot(x[low: high + 1], y[low: high + 1], zorder=1, label=count)

        ax.legend(loc="upper right", fontsize=3, framealpha=0.5)
        ax.scatter(x, y, s=4, c='r', zorder=2)
        ax.scatter(x[0], y[0], s=20, c='k', marker='*', zorder=3)

        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)

    plt.tight_layout()
    plt.savefig(save_path, bbox_inches='tight', dpi=400)
