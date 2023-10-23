"""
This script trains and tests a GraphSAGE model for node classification
on large graphs using GraphBolt dataloader.

Paper: [Inductive Representation Learning on Large Graphs]
(https://arxiv.org/abs/1706.02216)

Unlike previous dgl examples, we've utilized the newly defined dataloader
from GraphBolt. This example will help you grasp how to build an end-to-end
training pipeline using GraphBolt.

Before reading this example, please familiar yourself with graphsage node
classification by reading the example in the
`examples/core/graphsage/node_classification.py`. This introduction,
[A Blitz Introduction to Node Classification with DGL]
(https://docs.dgl.ai/tutorials/blitz/1_introduction.html), might be helpful.

If you want to train graphsage on a large graph in a distributed fashion,
please read the example in the `examples/distributed/graphsage/`.

This flowchart describes the main functional sequence of the provided example:
main
│
├───> OnDiskDataset pre-processing
│
├───> Instantiate SAGE model
│
├───> train
│     │
│     ├───> Get graphbolt dataloader (HIGHLIGHT)
│     │
│     └───> Training loop
│           │
│           ├───> SAGE.forward
│           │
│           └───> Validation set evaluation
│
└───> All nodes set inference & Test set evaluation
"""
import argparse

from typing import Literal

import dgl.graphbolt as gb
import dgl.nn as dglnn
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchmetrics.functional as MF
import tqdm


def create_dataloader(
    args, graph, features, itemset, job: Literal["train", "evaluate", "infer"]
):
    """
    [HIGHLIGHT]
    Get a GraphBolt version of a dataloader for node classification tasks.
    This function demonstrates how to utilize functional forms of datapipes in
    GraphBolt.
    Alternatively, you can create a datapipe using its class constructor.

    Parameters
    ----------
    args : Namespace
        The arguments parsed by `parser.parse_args()`.
    graph : SamplingGraph
        The network topology for sampling.
    features : FeatureStore
        The node features.
    itemset : Union[ItemSet, ItemSetDict]
        Data to be sampled.
    job : Literal["train", "evaluate", "infer"]
        The stage where dataloader is created, with options "train", "evaluate"
        and "infer".
    """

    ############################################################################
    # [Step-1]:
    # gb.ItemSampler()
    # [Input]:
    # 'itemset': The current dataset. (e.g. `train_set` or `valid_set`)
    # 'args.batch_size': Specify the number of samples to be processed together,
    # referred to as a 'mini-batch'. (The term 'mini-batch' is used here to
    # indicate a subset of the entire dataset that is processed together. This
    # is in contrast to processing the entire dataset, known as a 'full batch'.)
    # 'job': Determines whether data should be shuffled. (Shuffling is
    # generally used only in training to improve model generalization. It's
    # not used in validation and testing as the focus there is to evaluate
    # performance rather than to learn from the data.)
    # [Output]:
    # An ItemSampler object for handling mini-batch sampling.
    # [Role]:
    # Initialize the ItemSampler to sample mini-batche from the dataset.
    ############################################################################
    datapipe = gb.ItemSampler(
        itemset, batch_size=args.batch_size, shuffle=(job == "train")
    )

    ############################################################################
    # [Step-2]:
    # self.sample_neighbor()
    # [Input]:
    # 'graph': The network topology for sampling.
    # '[-1] or args.fanout': Number of neighbors to sample per node. In
    # training or validation, the length of args.fanout should be equal to the
    # number of layers in the model. In inference, this parameter is set to
    # [-1], indicating that all neighbors of a node are sampled.
    # [Output]:
    # A NeighborSampler object to sample neighbors.
    # [Role]:
    # Initialize a neighbor sampler for sampling the neighborhoods of nodes.
    ############################################################################
    datapipe = datapipe.sample_neighbor(
        graph, args.fanout if job != "infer" else [-1]
    )

    ############################################################################
    # [Step-3]:
    # self.fetch_feature()
    # [Input]:
    # 'features': The node features.
    # 'node_feature_keys': The keys of the node features to be fetched.
    # [Output]:
    # A FeatureFetcher object to fetch node features.
    # [Role]:
    # Initialize a feature fetcher for fetching features of the sampled
    # subgraphs. This step is skipped in inference because features are updated
    # as a whole during it, thus storing features in minibatch is unnecessary.
    ############################################################################
    if job != "infer":
        datapipe = datapipe.fetch_feature(features, node_feature_keys=["feat"])

    ############################################################################
    # [Step-4]:
    # self.to_dgl()
    # [Input]:
    # 'datapipe': The previous datapipe object.
    # [Output]:
    # A DGLMiniBatch used for computing.
    # [Role]:
    # Convert a mini-batch to dgl-minibatch.
    ############################################################################
    datapipe = datapipe.to_dgl()

    ############################################################################
    # [Step-5]:
    # gb.MultiProcessDataLoader()
    # [Input]:
    # 'datapipe': The datapipe object to be used for data loading.
    # 'args.num_workers': The number of processes to be used for data loading.
    # [Output]:
    # A MultiProcessDataLoader object to handle data loading.
    # [Role]:
    # Initialize a multi-process dataloader to load the data in parallel.
    ############################################################################
    dataloader = gb.MultiProcessDataLoader(
        datapipe, num_workers=args.num_workers
    )

    # Return the fully-initialized DataLoader object.
    return dataloader


class SAGE(nn.Module):
    def __init__(self, in_size, hidden_size, out_size):
        super().__init__()
        self.layers = nn.ModuleList()
        # Three-layer GraphSAGE-mean.
        self.layers.append(dglnn.SAGEConv(in_size, hidden_size, "mean"))
        self.layers.append(dglnn.SAGEConv(hidden_size, hidden_size, "mean"))
        self.layers.append(dglnn.SAGEConv(hidden_size, out_size, "mean"))
        self.dropout = nn.Dropout(0.5)
        self.hidden_size = hidden_size
        self.out_size = out_size
        # Set the dtype for the layers manually.
        self.set_layer_dtype(torch.float64)

    def set_layer_dtype(self, _dtype):
        for layer in self.layers:
            for param in layer.parameters():
                param.data = param.data.to(_dtype)

    def forward(self, blocks, x):
        hidden_x = x
        for layer_idx, (layer, block) in enumerate(zip(self.layers, blocks)):
            hidden_x = layer(block, hidden_x)
            is_last_layer = layer_idx == len(self.layers) - 1
            if not is_last_layer:
                hidden_x = F.relu(hidden_x)
                hidden_x = self.dropout(hidden_x)
        return hidden_x

    def inference(self, graph, features, dataloader):
        """Conduct layer-wise inference to get all the node embeddings."""
        feature = features.read("node", None, "feat")

        for layer_idx, layer in enumerate(self.layers):
            is_last_layer = layer_idx == len(self.layers) - 1

            y = torch.empty(
                graph.total_num_nodes,
                self.out_size if is_last_layer else self.hidden_size,
                dtype=torch.float64,
            )

            for step, data in tqdm.tqdm(enumerate(dataloader)):
                x = feature[data.input_nodes]
                hidden_x = layer(data.blocks[0], x)  # len(blocks) = 1
                if not is_last_layer:
                    hidden_x = F.relu(hidden_x)
                    hidden_x = self.dropout(hidden_x)
                # By design, our output nodes are contiguous.
                y[data.output_nodes[0] : data.output_nodes[-1] + 1] = hidden_x
            feature = y

        return y


@torch.no_grad()
def layerwise_infer(
    args, graph, features, test_set, all_nodes_set, model, num_classes
):
    model.eval()
    dataloader = create_dataloader(
        args, graph, features, all_nodes_set, job="infer"
    )
    pred = model.inference(graph, features, dataloader)
    pred = pred[test_set._items[0]]
    label = test_set._items[1].to(pred.device)

    return MF.accuracy(
        pred,
        label,
        task="multiclass",
        num_classes=num_classes,
    )


@torch.no_grad()
def evaluate(args, model, graph, features, itemset, num_classes):
    model.eval()
    y = []
    y_hats = []
    dataloader = create_dataloader(
        args, graph, features, itemset, job="evaluate"
    )

    for step, data in tqdm.tqdm(enumerate(dataloader)):
        x = data.node_features["feat"]
        y.append(data.labels)
        y_hats.append(model(data.blocks, x))

    return MF.accuracy(
        torch.cat(y_hats),
        torch.cat(y),
        task="multiclass",
        num_classes=num_classes,
    )


def train(args, graph, features, train_set, valid_set, num_classes, model):
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    dataloader = create_dataloader(
        args, graph, features, train_set, job="train"
    )

    for epoch in tqdm.trange(args.epochs):
        model.train()
        total_loss = 0
        for step, data in tqdm.tqdm(enumerate(dataloader)):
            # The input features from the source nodes in the first layer's
            # computation graph.
            x = data.node_features["feat"]

            # The ground truth labels from the destination nodes
            # in the last layer's computation graph.
            y = data.labels

            y_hat = model(data.blocks, x)

            # Compute loss.
            loss = F.cross_entropy(y_hat, y)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            total_loss += loss.item()

        # Evaluate the model.
        acc = evaluate(args, model, graph, features, valid_set, num_classes)
        print(
            f"Epoch {epoch:05d} | Loss {total_loss / (step + 1):.4f} | "
            f"Accuracy {acc.item():.4f} "
        )


def parse_args():
    parser = argparse.ArgumentParser(
        description="A script trains and tests a GraphSAGE model "
        "for node classification using GraphBolt dataloader."
    )
    parser.add_argument(
        "--epochs", type=int, default=10, help="Number of training epochs."
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=0.0005,
        help="Learning rate for optimization.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=256, help="Batch size for training."
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        default=4,
        help="Number of workers for data loading.",
    )
    parser.add_argument(
        "--fanout",
        type=str,
        default="15,10,5",
        help="Fan-out of neighbor sampling. It is IMPORTANT to keep len(fanout)"
        " identical with the number of layers in your model. Default: 15,10,5",
    )
    return parser.parse_args()


def main(args):
    # Load and preprocess dataset.
    dataset = gb.BuiltinDataset("ogbn-products").load()

    graph = dataset.graph
    features = dataset.feature
    train_set = dataset.tasks[0].train_set
    valid_set = dataset.tasks[0].validation_set
    test_set = dataset.tasks[0].test_set
    all_nodes_set = dataset.all_nodes_set
    args.fanout = list(map(int, args.fanout.split(",")))

    num_classes = dataset.tasks[0].metadata["num_classes"]

    in_size = features.size("node", None, "feat")[0]
    hidden_size = 128
    out_size = num_classes

    model = SAGE(in_size, hidden_size, out_size)

    # Model training.
    print("Training...")
    train(args, graph, features, train_set, valid_set, num_classes, model)

    # Test the model.
    print("Testing...")
    test_acc = layerwise_infer(
        args,
        graph,
        features,
        test_set,
        all_nodes_set,
        model,
        num_classes,
    )
    print(f"Test Accuracy is {test_acc.item():.4f}")


if __name__ == "__main__":
    args = parse_args()
    main(args)