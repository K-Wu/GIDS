import argparse, datetime
import dgl
import sklearn.metrics
import torch, torch.nn as nn, torch.optim as optim
import time, tqdm, numpy as np
from models import *
from dataloader import IGB260MDGLDataset, OGBDGLDataset
from dataloader import IGBHeteroDGLDataset, IGBHeteroDGLDatasetMassive, OGBHeteroDGLDatasetMassive

import csv 
import warnings

import torch.cuda.nvtx as t_nvtx
import nvtx
import threading
import gc

import GIDS

from ogb.graphproppred import DglGraphPropPredDataset
from ogb.nodeproppred import DglNodePropPredDataset, Evaluator

torch.manual_seed(0)
dgl.seed(0)
warnings.filterwarnings("ignore")


@nvtx.annotate("fetch_data_chunk()", color="blue")
def fetch_data_chunk(test, out_t, page_size, stream_id):
    test.fetch_from_backing_memory_chunk(out_t.data_ptr(), page_size, stream_id)


def print_times(transfer_time, train_time, e2e_time):
    print("transfer time: ", transfer_time)
    print("train time: ", train_time)
    print("e2e time: ", e2e_time)

def track_acc_BaM(g, args, device, label_array=None):
    GIDS_Loader = None
    GIDS_Loader = GIDS.GIDS(
        page_size = args.page_size,
        off = args.offset,
        num_ele = args.num_ele,
        num_ssd = args.num_ssd,
        cache_size = args.cache_size,
        wb_size = args.wb_size, #rename
        ssd_list=[5],
        cache_dim = args.cache_dim #should be removed
    )

    # Using BaM for the Graph Sampling Stage
    GIDS_Loader.init_graph_GIDS(512, 0, 512, 9 * 1000 * 1000 * 1000, 1)
    adj_mat =  g.adj_sparse('csc')
    GIDS_Loader.graph_GIDS.set_offsets(0,len(adj_mat[0]),len(adj_mat[0])+len(adj_mat[1]))

    sampler = dgl.dataloading.MultiLayerNeighborSampler(
               [int(fanout) for fanout in args.fan_out.split(',')],
                GIDS_flag=True, 
                GIDS=GIDS_Loader)

    g.ndata['features'] = g.ndata['feat']
    g.ndata['labels'] = g.ndata['label']

    train_nid = torch.nonzero(g.ndata['train_mask'], as_tuple=True)[0]
    val_nid = torch.nonzero(g.ndata['val_mask'], as_tuple=True)[0]
    test_nid = torch.nonzero(g.ndata['test_mask'], as_tuple=True)[0]
    in_feats = g.ndata['features'].shape[1]
    dim = args.dim


    train_dataloader = dgl.dataloading.DataLoader(
        g,
        train_nid,
        sampler,
        batch_size=args.batch_size,
        shuffle=True,
        drop_last=False,
        num_workers=args.num_workers,
        use_uva=False,
        use_uva_graph=False,    # Need to rename
        bam=True,
        feature_dim=dim,
        use_prefetch_thread=False,
        pin_prefetcher=False,
        window_buffer=False,
        use_alternate_streams=False,
        GIDS=GIDS_Loader
    )

    val_dataloader = dgl.dataloading.DataLoader(
        g, val_nid, sampler,
        batch_size=args.batch_size,
        shuffle=False, drop_last=False,
        num_workers=args.num_workers)

    test_dataloader = dgl.dataloading.DataLoader(
        g, test_nid, sampler,
        batch_size=args.batch_size,
        shuffle=True, drop_last=False,
        num_workers=args.num_workers)

    if args.model_type == 'gcn':
        model = GCN(in_feats, args.hidden_channels, args.num_classes, 
            args.num_layers).to(device)
    if args.model_type == 'sage':
        model = SAGE(in_feats, args.hidden_channels, args.num_classes, 
            args.num_layers).to(device)
    if args.model_type == 'gat':
        model = GAT(in_feats, args.hidden_channels, args.num_classes, 
            args.num_layers, args.num_heads).to(device)

    loss_fcn = nn.CrossEntropyLoss().to(device)
    optimizer = optim.Adam(
        model.parameters(), 
        lr=args.learning_rate, weight_decay=args.decay
        )

    # Setup is Done
    for epoch in tqdm.tqdm(range(args.epochs)):
        epoch_start = time.time()
        epoch_loss = 0
        train_acc = 0
        model.train()

        batch_input_time = 0
        train_time = 0
        transfer_time = 0
        e2e_time = 0
        e2e_time_start = time.time()

        for step, (input_nodes, seeds, blocks, ret) in enumerate(train_dataloader):
            
            if(step == 1000):
                print("warp up done")
                batch_input_time = 0
                transfer_time = 0
                train_time = 0
                e2e_time = 0
                e2e_time_start = time.time()
        
            
            # Features are fetched by the baseline GIDS dataloader in ret
            batch_inputs = ret
            batch_labels = blocks[-1].dstdata['labels']
            

            transfer_start = time.time() 
            blocks = [block.int().to(device) for block in blocks]
            batch_labels = batch_labels.to(device)
            transfer_time = transfer_time +  time.time()  - transfer_start
 
            # Model Training Stage
            train_start = time.time()
            batch_pred = model(blocks, batch_inputs)
            loss = loss_fcn(batch_pred, batch_labels)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            epoch_loss += loss.detach()                  
            train_time = train_time + time.time() - train_start
          
            if(step == 1100):
                print("Performance for 100 iteration after 1000 iteration")
                e2e_time += time.time() - e2e_time_start 
                train_dataloader.print_stats()
                train_dataloader.print_timer()
                print_times(transfer_time, train_time, e2e_time)
             
                batch_input_time = 0
                transfer_time = 0
                train_time = 0
                e2e_time = 0
                
                #Just testing 100 iterations remove the next line if you do not want to halt
                return None

  
    # Evaluation

    model.eval()
    predictions = []
    labels = []
    with torch.no_grad():
        for _, _, blocks,_ in test_dataloader:
            blocks = [block.to(device) for block in blocks]
            inputs = blocks[0].srcdata['feat']
     
            if(args.data == 'IGB'):
                labels.append(blocks[-1].dstdata['label'].cpu().numpy())
            elif(args.data == 'OGB'):
                out_label = torch.index_select(label_array, 0, b[1]).flatten()
                labels.append(out_label.numpy())
            predict = model(blocks, inputs).argmax(1).cpu().numpy()
            predictions.append(predict)

        predictions = np.concatenate(predictions)
        labels = np.concatenate(labels)
        test_acc = sklearn.metrics.accuracy_score(labels, predictions)*100
    print("Test Acc {:.2f}%".format(test_acc))



def track_acc_GIDS(g, category, args, device, label_array=None):

    GIDS_Loader = None
    GIDS_Loader = GIDS.GIDS(
        page_size = args.page_size,
        off = args.offset,
        num_ele = args.num_ele,
        num_ssd = args.num_ssd,
        cache_size = args.cache_size,
        window_buffer = args.window_buffer,
        wb_size = args.wb_size,
        accumulator_flag = args.accumulator,
        cache_dim = args.cache_dim,
        #ssd_list=[5],
        heterograph = True
    )
    
    dim = args.emb_size

    if(args.accumulator):
        GIDS_Loader.set_required_storage_access(args.bw, args.l_ssd, args.l_system, args.num_ssd, args.peak_percent)


    if(args.cpu_buffer):
        num_nodes = g.number_of_nodes()
        num_pinned_nodes = int(num_nodes * args.cpu_buffer_percent)
        GIDS_Loader.cpu_backing_buffer(dim, num_pinned_nodes)
        pr_ten = torch.load(args.pin_file)
        GIDS_Loader.set_cpu_buffer(pr_ten, num_pinned_nodes)


    sampler = dgl.dataloading.NeighborSampler([int(fanout) for fanout in args.fan_out.split(',')])


    # g.ndata['features'] = g.ndata['feat']
    # g.ndata['labels'] = g.ndata['label']

    train_nid = torch.nonzero(g.nodes[category].data['train_mask'], as_tuple=True)[0]
    val_nid = torch.nonzero(g.nodes[category].data['val_mask'], as_tuple=True)[0]
    test_nid = torch.nonzero(g.nodes[category].data['test_mask'], as_tuple=True)[0]
    
    in_feats = g.ndata['feat'][category].shape[1]

    if args.model_type == 'rgcn':
        model = RGCN(g.etypes, in_feats, args.hidden_channels,
            args.num_classes, args.num_layers).to(device)
    if args.model_type == 'rsage':
        model = RSAGE(g.etypes, in_feats, args.hidden_channels,
            args.num_classes, args.num_layers).to(device)
    if args.model_type == 'rgat':
        model = RGAT(g.etypes, in_feats, args.hidden_channels,
            args.num_classes, args.num_layers, args.num_heads).to(device)


    train_dataloader = dgl.dataloading.DataLoader(
        g,
        {category: train_nid},
        sampler,
        batch_size=args.batch_size,
        shuffle=True,
        drop_last=False,
        num_workers=args.num_workers,
        use_uva=False,
        feature_dim=dim,
        use_prefetch_thread=False,
        pin_prefetcher=False,
        use_alternate_streams=False,

        use_uva_graph=True,    # Need to rename
        bam=True,
        #window_buffer=args.window_buffer,
        window_buffer=False,
        window_buffer_size=args.wb_size,
        GIDS=GIDS_Loader
    )

    val_dataloader = dgl.dataloading.DataLoader(
        g, {category: val_nid}, sampler,
        batch_size=args.batch_size,
        shuffle=False, drop_last=False,
        num_workers=args.num_workers)

    test_dataloader = dgl.dataloading.DataLoader(
        g, {category: test_nid}, sampler,
        batch_size=args.batch_size,
        shuffle=True, drop_last=False,
        num_workers=args.num_workers)


    loss_fcn = nn.CrossEntropyLoss().to(device)
    optimizer = optim.Adam(
        model.parameters(), 
        lr=args.learning_rate, weight_decay=args.decay
        )

    warm_up_iter = 100
    # Setup is Done
    for epoch in tqdm.tqdm(range(args.epochs)):
        epoch_start = time.time()
        epoch_loss = 0
        train_acc = 0
        model.train()

        batch_input_time = 0
        train_time = 0
        transfer_time = 0
        e2e_time = 0
        e2e_time_start = time.time()

        for step, (input_nodes, seeds, blocks, ret) in enumerate(train_dataloader):
            if(step % 10 == 0):
                print("step: ", step)
            if(step == warm_up_iter):
                print("warp up done")
                train_dataloader.print_stats()
                train_dataloader.print_timer()
                batch_input_time = 0
                transfer_time = 0
                train_time = 0
                e2e_time = 0
                e2e_time_start = time.time()
        
            
            # Features are fetched by the baseline GIDS dataloader in ret

            batch_inputs = ret
            transfer_start = time.time() 

            batch_labels = blocks[-1].dstdata['label']['paper'] 
           
            blocks = [block.int().to(device) for block in blocks]
            transfer_time = transfer_time +  time.time()  - transfer_start
 
            # Model Training Stage
            train_start = time.time()
            batch_pred = model(blocks, batch_inputs)
            loss = loss_fcn(batch_pred, batch_labels)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            epoch_loss += loss.detach()                  
            train_time = train_time + time.time() - train_start
          
            if(step == warm_up_iter + 100):
                print("Performance for 100 iteration after 1000 iteration")
                e2e_time += time.time() - e2e_time_start 
                train_dataloader.print_stats()
                train_dataloader.print_timer()
                print_times(transfer_time, train_time, e2e_time)
             
                batch_input_time = 0
                transfer_time = 0
                train_time = 0
                e2e_time = 0
                
                #Just testing 100 iterations remove the next line if you do not want to halt
                return None


       
  



    
       

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    # Loading dataset
    parser.add_argument('--path', type=str, default='/mnt/nvme14/IGB260M', 
        help='path containing the datasets')
    parser.add_argument('--dataset_size', type=str, default='experimental',
        choices=['experimental', 'small', 'medium', 'large', 'full'], 
        help='size of the datasets')
    parser.add_argument('--num_classes', type=int, default=19, 
        choices=[19, 2983, 172, 348,349, 350, 153, 152], help='number of classes')
    parser.add_argument('--in_memory', type=int, default=0, 
        choices=[0, 1], help='0:read only mmap_mode=r, 1:load into memory')
    parser.add_argument('--synthetic', type=int, default=0,
        choices=[0, 1], help='0:nlp-node embeddings, 1:random')
    parser.add_argument('--data', type=str, default='IGB')
    parser.add_argument('--emb_size', type=int, default=1024)
    
    # Model
    parser.add_argument('--model_type', type=str, default='gcn',
                        choices=['rgat', 'rsage', 'rgcn'])
    parser.add_argument('--modelpath', type=str, default='deletethis.pt')
    parser.add_argument('--model_save', type=int, default=0)

    # Model parameters 
    parser.add_argument('--fan_out', type=str, default='10,15')
    parser.add_argument('--batch_size', type=int, default=1024)
    parser.add_argument('--num_workers', type=int, default=0)
    parser.add_argument('--hidden_channels', type=int, default=128)
    parser.add_argument('--learning_rate', type=float, default=0.01)
    parser.add_argument('--decay', type=float, default=0.001)
    parser.add_argument('--epochs', type=int, default=3)
    parser.add_argument('--num_layers', type=int, default=6)
    parser.add_argument('--num_heads', type=int, default=4)
    parser.add_argument('--log_every', type=int, default=2)

    #GIDS parameter
    parser.add_argument('--GIDS', action='store_true', help='Enable GIDS Dataloader')
    parser.add_argument('--num_ssd', type=int, default=1)
    parser.add_argument('--cache_size', type=int, default=8)
    parser.add_argument('--uva', type=int, default=0)
    parser.add_argument('--uva_graph', type=int, default=0)
    parser.add_argument('--wb_size', type=int, default=6)

    parser.add_argument('--device', type=int, default=0)

    #GIDS Optimization
    parser.add_argument('--accumulator', action='store_true', help='Enable Storage Access Accmulator')
    parser.add_argument('--bw', type=float, default=5.8, help='SSD peak bandwidth in GB/s')
    parser.add_argument('--l_ssd', type=float, default=11.0, help='SSD latency in microseconds')
    parser.add_argument('--l_system', type=float, default=20.0, help='System latency in microseconds')
    parser.add_argument('--peak_percent', type=float, default=0.95)

    parser.add_argument('--num_iter', type=int, default=1)

    parser.add_argument('--cpu_buffer', action='store_true', help='Enable CPU Feature Buffer')
    parser.add_argument('--cpu_buffer_percent', type=float, default=0.2, help='CPU feature buffer size (0.1 for 10%)')
    parser.add_argument('--pin_file', type=str, default="/mnt/nvme16/pr_full.pt", 
        help='Pytorch Tensor File for the list of nodes that will be pinned in the CPU feature buffer')

    parser.add_argument('--window_buffer', action='store_true', help='Enable Window Buffering')



    #GPU Software Cache Parameters
    parser.add_argument('--page_size', type=int, default=8)
    parser.add_argument('--offset', type=int, default=0, help='Offset for the feature data stored in the SSD') 
    parser.add_argument('--num_ele', type=int, default=100, help='Number of elements in the dataset (Total Size / sizeof(Type)') 
    parser.add_argument('--cache_dim', type=int, default=1024) #CHECK


    args = parser.parse_args()
    print("GIDS DataLoader Setting")
    print("GIDS: ", args.GIDS)
    print("CPU Feature Buffer: ", args.cpu_buffer)
    print("Window Buffering: ", args.window_buffer)
    print("Storage Access Accumulator: ", args.accumulator)

    labels = None
    device = f'cuda:' + str(args.device) if torch.cuda.is_available() else 'cpu'
    if(args.data == 'IGB'):
        print("Dataset: IGB")
        if(args.dataset_size == 'full' or args.dataset_size == 'large'):
            dataset = IGBHeteroDGLDatasetMassive(args)
        else:
            dataset = IGBHeteroDGLDataset(args)
        g = dataset[0]
        g = g.formats('csc')
    elif(args.data == "OGB"):
        print("Dataset: OGB")
        dataset = OGBHeteroDGLDatasetMassive(args)
        g = dataset[0]
        g = g.formats('csc')
    else:
        g=None
        dataset=None
    
    category = g.predict
    track_acc_GIDS(g, category, args, device, labels)
    #track_acc(g, args, device, labels)




