'''
saving the features generated from multiple images to a folder
'''
import torch
import torch.nn as nn
from tqdm import tqdm
import json
import os
import gc
import numpy as np

from torch.utils.data import DataLoader

import argparse
from src.utils import setup_seed, multi_acc
from src.pixel_classifier import  load_ensemble, compute_iou, predict_labels, save_predictions, save_predictions, pixel_classifier
from src.datasets import ImageLabelDataset, FeatureDataset, ImageHardLabelDataset, make_transform
from src.feature_extractors import create_feature_extractor, collect_features

from guided_diffusion.guided_diffusion.script_util import model_and_diffusion_defaults, add_dict_to_argparser
from guided_diffusion.guided_diffusion.dist_util import dev


# change labels to the hard attention mode
def prepare_data(args):
    feature_extractor = create_feature_extractor(**args)
    
    print(f"Preparing the train set for {args['category']}...")

    if args['hard_attention']:
        dataset = ImageHardLabelDataset(
            data_dir=args['training_path'],
            resolution=args['image_size'],
            num_images=args['training_number'],
            transform=make_transform(
                args['model_type'],
                args['image_size']
            )
        )
    else:
        dataset = ImageLabelDataset(
            data_dir=args['training_path'],
            resolution=args['image_size'],
            num_images=args['training_number'],
            transform=make_transform(
                args['model_type'],
                args['image_size']
            )
        )
    # X = torch.zeros((len(dataset), *args['dim'][::-1]), dtype=torch.float)
    # y = torch.zeros((len(dataset), *args['dim'][:-1]), dtype=torch.uint8)
    X = []
    y = []

    if 'share_noise' in args and args['share_noise']:
        rnd_gen = torch.Generator(device=dev()).manual_seed(args['seed'])
        noise = torch.randn(1, 3, args['image_size'], args['image_size'], 
                            generator=rnd_gen, device=dev())
    else:
        noise = None 

    num = 0
    for row, (img, label) in enumerate(tqdm(dataset)):
        img = img[None].to(dev())
        # print(img.shape, noise.shape)
        # print(args['training_path'])
        features = feature_extractor(img, noise=noise)
        collected_features = collect_features(args, features)
        # collected_features dimension [concat_dim, x_dim, y_dim]
        label = label.to(torch.long)
        X_x = label[1,:]
        X_y = label[0,:]
        # print(collected_features.shape, torch.max(X_x), torch.max(X_y))
        # X.append(collected_features[:,X_x,X_y].cpu())
        X_save = collected_features[:,X_x,X_y].cpu()
        for feature in X_save:
            torch.save(feature, os.path.join(args['training_path_save_feature'],f'{num}.pt'))
        # X[row] = collect_features(args, features).cpu()
        
        # for target in range(args['number_class']):
        #     if target == args['ignore_label']: continue
        #     if 0 < (label == target).sum() < 20:
        #         print(f'Delete small annotation from image {dataset.image_paths[row]} | label {target}')
        #         label[label == target] = args['ignore_label']
        # y[row] = label
        y.append(label[2,:])
    
    # d = X.shape[1]
    # X = torch.cat(X, dim=1)
    y = torch.cat(y, dim=0)
    torch.save(y, os.path.join(args['training_path_save_feature'],'index_label.pt'))
    # print(torch.max(y))
    # d = collected_features.shape[0]
    # print(f'Total dimension {d}')
    # # X = X.permute(1,0,2,3).reshape(d, -1).permute(1, 0)
    # X = X.reshape(d, -1).permute(1, 0)
    # # y = y.flatten()
    return X[y != args['ignore_label']], y[y != args['ignore_label']]


def evaluation(args, models):
    feature_extractor = create_feature_extractor(**args)
    print('create finished')
    if args['hard_attention']:
        dataset = ImageHardLabelDataset(
            data_dir=args['testing_path'],
            resolution=args['image_size'],
            num_images=args['testing_number'],
            transform=make_transform(
                args['model_type'],
                args['image_size']
            )
        )
    else:
        dataset = ImageLabelDataset(
            data_dir=args['testing_path'],
            resolution=args['image_size'],
            num_images=args['testing_number'],
            transform=make_transform(
                args['model_type'],
                args['image_size']
            )
        )

    if 'share_noise' in args and args['share_noise']:
        rnd_gen = torch.Generator(device=dev()).manual_seed(args['seed'])
        noise = torch.randn(1, 3, args['image_size'], args['image_size'], 
                            generator=rnd_gen, device=dev())
    else:
        noise = None 

    preds, gts, uncertainty_scores = [], [], []
    for img, label in tqdm(dataset):        
        img = img[None].to(dev())
        features = feature_extractor(img, noise=noise)
        collected_features = collect_features(args, features)
        label = label.to(torch.long)
        X_x = label[1,:]
        X_y = label[0,:]
        x = collected_features[:,X_x,X_y]

        # x = features.view(args['dim'][-1], -1).permute(1, 0)
        x = x.permute(1, 0)
        pred, uncertainty_score = predict_labels(
            models, x, size=args['dim'][:-1]
        )
        label = label[2,:]
        gts.append(label.numpy())
        preds.append(pred.numpy())
        uncertainty_scores.append(uncertainty_score.item())
    
    save_predictions(args, dataset.image_paths, preds)
    miou = compute_iou(args, preds, gts)
    mean_acc = np.mean([np.sum(preds[i]==gts[i])/len(preds[i]) for i in range(len(preds))])
    print(f'Mean Accuracy: ', mean_acc)
    print(f'Overall mIoU: ', miou)
    print(f'Mean uncertainty: {sum(uncertainty_scores) / len(uncertainty_scores)}')


# Adopted from https://github.com/nv-tlabs/datasetGAN_release/blob/d9564d4d2f338eaad78132192b865b6cc1e26cac/datasetGAN/train_interpreter.py#L434
def train(args):
    # C, H*W
    features, labels = prepare_data(args)
    # print(torch.max(labels))
    # train_data = FeatureDataset(features, labels)

    # print(f" ********* max_label {args['number_class']} *** ignore_label {args['ignore_label']} ***********")
    # print(f" *********************** Current number data {len(features)} ***********************")

    # train_loader = DataLoader(dataset=train_data, batch_size=args['batch_size'], shuffle=True, drop_last=True)

    # print(" *********************** Current dataloader length " +  str(len(train_loader)) + " ***********************")
    # for MODEL_NUMBER in range(args['start_model_num'], args['model_num'], 1):

    #     gc.collect()
    #     classifier = pixel_classifier(numpy_class=(args['number_class']), dim=args['dim'][-1])
    #     classifier.init_weights()

    #     classifier = nn.DataParallel(classifier).cuda()
    #     criterion = nn.CrossEntropyLoss()
    #     optimizer = torch.optim.Adam(classifier.parameters(), lr=0.001)
    #     classifier.train()

    #     iteration = 0
    #     break_count = 0
    #     best_loss = 10000000
    #     stop_sign = 0
    #     for epoch in range(100):
    #         for X_batch, y_batch in train_loader:
    #             X_batch, y_batch = X_batch.to(dev()), y_batch.to(dev())
    #             y_batch = y_batch.type(torch.long)

    #             optimizer.zero_grad()
    #             y_pred = classifier(X_batch)
    #             # print(y_pred.shape, y_batch.shape, torch.max(y_batch))
    #             loss = criterion(y_pred, y_batch)
    #             acc = multi_acc(y_pred, y_batch)

    #             loss.backward()
    #             optimizer.step()

    #             iteration += 1
    #             if iteration % 1000 == 0:
    #                 print('Epoch : ', str(epoch), 'iteration', iteration, 'loss', loss.item(), 'acc', acc)
                
    #             if epoch > 3:
    #                 if loss.item() < best_loss:
    #                     best_loss = loss.item()
    #                     break_count = 0
    #                 else:
    #                     break_count += 1

    #                 if break_count > 50:
    #                     stop_sign = 1
    #                     print("*************** Break, Total iters,", iteration, ", at epoch", str(epoch), "***************")
    #                     break

    #         if stop_sign == 1:
    #             break

    #     model_path = os.path.join(args['exp_dir'], 
    #                               'model_' + str(MODEL_NUMBER) + '.pth')
    #     MODEL_NUMBER += 1
    #     print('save to:',model_path)
    #     torch.save({'model_state_dict': classifier.state_dict()},
    #                model_path)
    

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    add_dict_to_argparser(parser, model_and_diffusion_defaults())

    parser.add_argument('--exp', type=str)
    parser.add_argument('--seed', type=int,  default=0)

    args = parser.parse_args()
    setup_seed(args.seed)

    # Load the experiment config
    opts = json.load(open(args.exp, 'r'))
    opts.update(vars(args))
    opts['image_size'] = opts['dim'][0]

    # Prepare the experiment folder 
    if len(opts['steps']) > 0:
        suffix = '_'.join([str(step) for step in opts['steps']])
        suffix += '_' + '_'.join([str(step) for step in opts['blocks']])
        opts['exp_dir'] = os.path.join(opts['exp_dir'], suffix)

    path = opts['exp_dir']
    os.makedirs(path, exist_ok=True)
    print('Experiment folder: %s' % (path))
    os.system('cp %s %s' % (args.exp, opts['exp_dir']))

    # Check whether all models in ensemble are trained 
    pretrained = [os.path.exists(os.path.join(opts['exp_dir'], f'model_{i}.pth')) 
                  for i in range(opts['model_num'])]
              
    if not all(pretrained):
        # train all remaining models
        opts['start_model_num'] = sum(pretrained)
        train(opts)
    
    # print('Loading pretrained models...')
    # models = load_ensemble(opts, device='cuda')
    # evaluation(opts, models)
