# -*- coding:utf-8 -*-
import os
import torch 
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision.transforms as transforms
#from data.cifar import CIFAR10, CIFAR100
#from data.brain import CustomDataset
#from data.mnist import MNIST
from model import CNN
import argparse, sys
import numpy as np
import datetime
import shutil
from data.loader import Clover #dataset class
from data.aptos_loader import APTOS #aptos loader class

from loss import loss_coteaching

parser = argparse.ArgumentParser()
parser.add_argument('--lr', type = float, default = 0.001)
parser.add_argument('--result_dir', type = str, help = 'dir to save result txt files', default = 'results/')
parser.add_argument('--noise_rate', type = float, help = 'corruption rate, should be less than 1', default = 0.2)
parser.add_argument('--forget_rate', type = float, help = 'forget rate', default = None)
parser.add_argument('--noise_type', type = str, help='[pairflip, symmetric]', default='pairflip')
parser.add_argument('--num_gradual', type = int, default = 10, help='how many epochs for linear drop rate, can be 5, 10, 15. This parameter is equal to Tk for R(T) in Co-teaching paper.')
parser.add_argument('--exponent', type = float, default = 1, help='exponent of the forget rate, can be 0.5, 1, 2. This parameter is equal to c in Tc for R(T) in Co-teaching paper.')
parser.add_argument('--top_bn', action='store_true')
parser.add_argument('--dataset', type = str, help = 'mnist, cifar10, or cifar100', default = 'mnist')
parser.add_argument('--n_epoch', type=int, default=200)
parser.add_argument('--seed', type=int, default=1)
parser.add_argument('--print_freq', type=int, default=50)
parser.add_argument('--num_workers', type=int, default=4, help='how many subprocesses to use for data loading')
parser.add_argument('--num_iter_per_epoch', type=int, default=400)
parser.add_argument('--epoch_decay_start', type=int, default=80)

args = parser.parse_args()

import matplotlib.pyplot as plt

def show_images(images, labels, class_names, num_images=5):
    """
    Display a few images from the dataset.

    Args:
        images (torch.Tensor): Batch of images.
        labels (torch.Tensor): Corresponding labels.
        class_names (list): List of class names.
        num_images (int): Number of images to display.
    """
    plt.figure(figsize=(10, 10))
    for i in range(num_images):
        plt.subplot(1, num_images, i + 1)
        image = images[i].numpy().transpose((1, 2, 0))  # Convert from CxHxW to HxWxC
        plt.imshow(image)
        plt.title(f"Label: {class_names[labels[i]]}")
        plt.axis('off')
    plt.show()

# Seed
torch.manual_seed(args.seed)
torch.cuda.manual_seed(args.seed)

# Hyper Parameters
batch_size = 64
learning_rate = args.lr 


'''    
# Define transformations (including resizing)
transform = transforms.Compose([
    transforms.Resize((224, 224)),  # Resize images to 224x224
    transforms.ToTensor(),          # Convert images to PyTorch tensors
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])  # Normalize
])
'''

# load dataset
if args.dataset == 'mri':
    input_channel = 3  # RGB images, so 3 channels
    num_classes = 2  # Binary classification, so 2 classes
    args.top_bn = False
    args.epoch_decay_start = 75
    args.n_epoch = 125
    
    # Adjust the paths to where your dataset is stored
    train_dataset = Clover(
        root='./data/mri',
        train=True,
        transform=transforms.ToTensor(),
        noise_type=args.noise_type,
        noise_rate=args.noise_rate,
        nb_classes=2
    )
    
    test_dataset = Clover(
        root='./data/mri',
        train=False,
        transform=transforms.ToTensor(),
        noise_type=args.noise_type,
        noise_rate=args.noise_rate
    )

if args.dataset == 'aptos':
    input_channel = 3  # RGB images, so 3 channels
    num_classes = 5  # APTOS dataset has 5 classes
    args.top_bn = False
    args.epoch_decay_start = 75
    args.n_epoch = 125

    # Define transforms for training and testing
    train_transform = transforms.Compose([
        transforms.RandomHorizontalFlip(),
        transforms.RandomVerticalFlip(),
        transforms.ToTensor(),
    ])
    test_transform = transforms.Compose([
        transforms.ToTensor(),
    ])

    # Load training dataset
    train_dataset = APTOS(
        root='./dataset/aptos_coco',  # Path to the dataset root directory
        csv_file='./dataset/aptos_coco/train.csv',  # Path to the training CSV file
        train=True,  # Training set
        transform=train_transform,  # Apply training transforms
        noise_type=args.noise_type,  # Type of noise to inject
        noise_rate=args.noise_rate,  # Noise rate (e.g., 0.2 for 20% noise)
        nb_classes=num_classes  # Number of classes
    )

    # Load test dataset
    test_dataset = APTOS(
        root='./dataset/aptos_coco',  # Path to the dataset root directory
        csv_file='./dataset/aptos_coco/test.csv',  # Path to the test CSV file
        train=False,  # Test set
        transform=test_transform,  # Apply test transforms
        noise_type=None,  # No noise for the test set
        noise_rate=0  # No noise for the test set
    )

print(len(train_dataset))
print(len(test_dataset))
print(type(train_dataset))
print(type(test_dataset))

if args.forget_rate is None:
    forget_rate=args.noise_rate
else:
    forget_rate=args.forget_rate

noise_or_not = train_dataset.noise_or_not

# Adjust learning rate and betas for Adam Optimizer
mom1 = 0.9
mom2 = 0.1
alpha_plan = [learning_rate] * args.n_epoch
beta1_plan = [mom1] * args.n_epoch
for i in range(args.epoch_decay_start, args.n_epoch):
    alpha_plan[i] = float(args.n_epoch - i) / (args.n_epoch - args.epoch_decay_start) * learning_rate
    beta1_plan[i] = mom2

def adjust_learning_rate(optimizer, epoch):
    for param_group in optimizer.param_groups:
        param_group['lr']=alpha_plan[epoch]
        param_group['betas']=(beta1_plan[epoch], 0.999) # Only change beta1
        
# define drop rate schedule
rate_schedule = np.ones(args.n_epoch)*forget_rate
rate_schedule[:args.num_gradual] = np.linspace(0, forget_rate**args.exponent, args.num_gradual)
   
save_dir = args.result_dir +'/' +args.dataset+'/coteaching/'

if not os.path.exists(save_dir):
    os.system('mkdir -p %s' % save_dir)

model_str=args.dataset+'_coteaching_'+args.noise_type+'_'+str(args.noise_rate)

txtfile=save_dir+"/"+model_str+".txt"
nowTime=datetime.datetime.now().strftime('%Y-%m-%d-%H:%M:%S')
if os.path.exists(txtfile):
    os.system('mv %s %s' % (txtfile, txtfile+".bak-%s" % nowTime))


def accuracy(logit, target, topk=(1,)):
    """
    Computes the precision@k for the specified values of k.
    Handles cases where the number of classes is smaller than max(topk).
    """
    output = F.softmax(logit, dim=1)  # Apply softmax to logits
    maxk = min(max(topk), output.size(1))  # Ensure maxk does not exceed the number of classes
    batch_size = target.size(0)

    # If maxk is 0 (e.g., no classes), return 0 accuracy for all topk
    if maxk == 0:
        return [torch.tensor(0.0, device=logit.device) for _ in topk]

    _, pred = output.topk(maxk, 1, True, True)  # Get the top maxk predictions
    pred = pred.t()  # Transpose to shape (maxk, batch_size)
    correct = pred.eq(target.reshape(1, -1).expand_as(pred))  # Compare predictions to targets

    res = []
    for k in topk:
        if k > maxk:  # If k exceeds the number of classes, set accuracy to 0
            res.append(torch.tensor(0.0, device=logit.device))
        else:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)  # Count correct predictions
            res.append(correct_k.mul_(100.0 / batch_size))  # Compute accuracy as a percentage
    return res

# Train the Model
def train(train_loader,epoch, model1, optimizer1, model2, optimizer2):
    print('Training %s...', model_str)
    pure_ratio_list=[]
    pure_ratio_1_list=[]
    pure_ratio_2_list=[]
    
    train_total=0
    train_correct=0 
    train_total2=0
    train_correct2=0 

    for i, (images, labels, indexes) in enumerate(train_loader):
        ind=indexes.cpu().numpy().transpose()
        if i>args.num_iter_per_epoch:
            break
      
        images = Variable(images).cuda()
        labels = Variable(labels).cuda()
        
        # Forward + Backward + Optimize
        logits1=model1(images)
        prec1, _ = accuracy(logits1, labels, topk=(1, 5))
        train_total+=1
        train_correct+=prec1

        logits2 = model2(images)
        prec2, _ = accuracy(logits2, labels, topk=(1, 5))
        train_total2+=1
        train_correct2+=prec2
        loss_1, loss_2, pure_ratio_1, pure_ratio_2 = loss_coteaching(logits1, logits2, labels, rate_schedule[epoch], ind, noise_or_not)
        pure_ratio_1_list.append(100*pure_ratio_1)
        pure_ratio_2_list.append(100*pure_ratio_2)

        optimizer1.zero_grad()
        loss_1.backward()
        optimizer1.step()
        optimizer2.zero_grad()
        loss_2.backward()
        optimizer2.step()
        if (i+1) % args.print_freq == 0:
            print ('Epoch [%d/%d], Iter [%d/%d] Training Accuracy1: %.4F, Training Accuracy2: %.4f, Loss1: %.4f, Loss2: %.4f, Pure Ratio1: %.4f, Pure Ratio2 %.4f' 
                  %(epoch+1, args.n_epoch, i+1, len(train_dataset)//batch_size, prec1, prec2, loss_1.item(), loss_2.item(), np.sum(pure_ratio_1_list)/len(pure_ratio_1_list), np.sum(pure_ratio_2_list)/len(pure_ratio_2_list)))

    train_acc1=float(train_correct)/float(train_total)
    train_acc2=float(train_correct2)/float(train_total2)
    return train_acc1, train_acc2, pure_ratio_1_list, pure_ratio_2_list

# Evaluate the Model
def evaluate(test_loader, model1, model2):
    print('Evaluating %s...',  model_str)
    model1.eval()    # Change model to 'eval' mode.
    correct1 = 0
    total1 = 0
    for images, labels, _ in test_loader:
        images = Variable(images).cuda()
        logits1 = model1(images)
        outputs1 = F.softmax(logits1, dim=1)
        _, pred1 = torch.max(outputs1.data, 1)
        total1 += labels.size(0)
        correct1 += (pred1.cpu() == labels).sum()

    model2.eval()    # Change model to 'eval' mode 
    correct2 = 0
    total2 = 0
    for images, labels, _ in test_loader:
        images = Variable(images).cuda()
        logits2 = model2(images)
        outputs2 = F.softmax(logits2, dim=1)
        _, pred2 = torch.max(outputs2.data, 1)
        total2 += labels.size(0)
        correct2 += (pred2.cpu() == labels).sum()
 
    acc1 = 100*float(correct1)/float(total1)
    acc2 = 100*float(correct2)/float(total2)
    return acc1, acc2

'''
def main():
    # Data Loader (Input Pipeline)
    print ('loading dataset...')
    train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                               batch_size=batch_size, 
                                               num_workers=args.num_workers,
                                               drop_last=True,
                                               shuffle=True)
    
    test_loader = torch.utils.data.DataLoader(dataset=test_dataset,
                                              batch_size=batch_size, 
                                              num_workers=args.num_workers,
                                              drop_last=True,
                                              shuffle=False)
    # Define models
    print ('building model...')
    cnn1 = CNN(input_channel=input_channel, n_outputs=num_classes)
    cnn1.cuda()
    print(cnn1.parameters)
    optimizer1 = torch.optim.Adam(cnn1.parameters(), lr=learning_rate)
    
    cnn2 = CNN(input_channel=input_channel, n_outputs=num_classes)
    cnn2.cuda()
    print(cnn2.parameters)
    optimizer2 = torch.optim.Adam(cnn2.parameters(), lr=learning_rate)

    mean_pure_ratio1=0
    mean_pure_ratio2=0

    with open(txtfile, "a") as myfile:
        myfile.write('epoch: train_acc1 train_acc2 test_acc1 test_acc2 pure_ratio1 pure_ratio2\n')

    epoch=0
    train_acc1=0
    train_acc2=0
    # evaluate models with random weights
    #test_acc1, test_acc2=evaluate(test_loader, cnn1, cnn2)
    #print('Epoch [%d/%d] Test Accuracy on the %s test images: Model1 %.4f %% Model2 %.4f %% Pure Ratio1 %.4f %% Pure Ratio2 %.4f %%' % (epoch+1, args.n_epoch, len(test_dataset), test_acc1, test_acc2, mean_pure_ratio1, mean_pure_ratio2))
    # save results
    with open(txtfile, "a") as myfile:
        myfile.write(str(int(epoch)) + ': '  + str(train_acc1) +' '  + str(train_acc2) +' '  + str(test_acc1) + " " + str(test_acc2) + ' '  + str(mean_pure_ratio1) + ' '  + str(mean_pure_ratio2) + "\n")

    # training
    for epoch in range(1, args.n_epoch):
        # train models
        cnn1.train()
        adjust_learning_rate(optimizer1, epoch)
        cnn2.train()
        adjust_learning_rate(optimizer2, epoch)
        train_acc1, train_acc2, pure_ratio_1_list, pure_ratio_2_list=train(train_loader, epoch, cnn1, optimizer1, cnn2, optimizer2)
        # evaluate models
        test_acc1, test_acc2=evaluate(test_loader, cnn1, cnn2)
        # save results
        mean_pure_ratio1 = sum(pure_ratio_1_list)/len(pure_ratio_1_list)
        mean_pure_ratio2 = sum(pure_ratio_2_list)/len(pure_ratio_2_list)
        print('Epoch [%d/%d] Test Accuracy on the %s test images: Model1 %.4f %% Model2 %.4f %%, Pure Ratio 1 %.4f %%, Pure Ratio 2 %.4f %%' % (epoch+1, args.n_epoch, len(test_dataset), test_acc1, test_acc2, mean_pure_ratio1, mean_pure_ratio2))
        with open(txtfile, "a") as myfile:
            myfile.write(str(int(epoch)) + ': '  + str(train_acc1) +' '  + str(train_acc2) +' '  + str(test_acc1) + " " + str(test_acc2) + ' ' + str(mean_pure_ratio1) + ' ' + str(mean_pure_ratio2) + "\n")
'''

def main():
    # Data Loader (Input Pipeline)
    print('loading dataset...')
    image, label, index = train_dataset[0]
    print(f"Image shape: {image.shape}")  # Should be (C, H, W), e.g., (3, 128, 128) for RGB
    print(f"Label: {label}")              # Should be an integer (0 to num_classes-1)
    print(len(train_dataset))
    print(len(test_dataset))
    train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                               batch_size=batch_size, 
                                               num_workers=args.num_workers,
                                               drop_last=True,
                                               shuffle=True)
    

# Check the shape of the first image and label
    image, label, index = train_dataset[0]
    print(f"Image shape: {image.shape}")  # Should be (C, H, W), e.g., (3, 128, 128) for RGB
    print(f"Label: {label}")              # Should be an integer (0 to num_classes-1)
    print(len(train_dataset))
    print(len(test_dataset))
    # Access a batch of data
    train_images, train_labels, _ = next(iter(train_loader))
    print(train_images.shape)  # Should print torch.Size([32, 3, 224, 224])
    
    test_loader = torch.utils.data.DataLoader(dataset=test_dataset,
                                              batch_size=batch_size, 
                                              num_workers=args.num_workers,
                                              drop_last=True,
                                              shuffle=False)

    # Define models
    print('building model...')
    cnn1 = CNN(input_channel=input_channel, n_outputs=num_classes)
    cnn1.cuda()
    print(cnn1.parameters)
    optimizer1 = torch.optim.Adam(cnn1.parameters(), lr=learning_rate)
    
    cnn2 = CNN(input_channel=input_channel, n_outputs=num_classes)
    cnn2.cuda()
    print(cnn2.parameters)
    optimizer2 = torch.optim.Adam(cnn2.parameters(), lr=learning_rate)

    mean_pure_ratio1 = 0
    mean_pure_ratio2 = 0

    with open(txtfile, "a") as myfile:
        myfile.write('epoch: train_acc1 train_acc2 test_acc1 test_acc2 pure_ratio1 pure_ratio2\n')

    epoch = 0
    train_acc1 = 0
    train_acc2 = 0
    test_acc1 = 0  # Initialize test_acc1
    test_acc2 = 0  # Initialize test_acc2

    # Evaluate models with random weights
    #test_acc1, test_acc2 = evaluate(test_loader, cnn1, cnn2)
    #print('Epoch [%d/%d] Test Accuracy on the %s test images: Model1 %.4f %% Model2 %.4f %% Pure Ratio1 %.4f %% Pure Ratio2 %.4f %%' % (epoch+1, args.n_epoch, len(test_dataset), test_acc1, test_acc2, mean_pure_ratio1, mean_pure_ratio2))
    
    # Save results
    with open(txtfile, "a") as myfile:
        myfile.write(str(int(epoch)) + ': '  + str(train_acc1) +' '  + str(train_acc2) +' '  + str(test_acc1) + " " + str(test_acc2) + ' '  + str(mean_pure_ratio1) + ' '  + str(mean_pure_ratio2) + "\n")

    # Training loop
    for epoch in range(1, args.n_epoch):
        # Train models
        cnn1.train()
        #print("cnn1 happens")
        adjust_learning_rate(optimizer1, epoch)
        cnn2.train()
        #print("cnn2 happens")
        adjust_learning_rate(optimizer2, epoch)
        train_acc1, train_acc2, pure_ratio_1_list, pure_ratio_2_list = train(train_loader, epoch, cnn1, optimizer1, cnn2, optimizer2)
        print("training finished")
        print("now evaluation")
        # Evaluate models
        test_acc1, test_acc2 = evaluate(test_loader, cnn1, cnn2)
        
        # Calculate mean pure ratios
        mean_pure_ratio1 = sum(pure_ratio_1_list) / len(pure_ratio_1_list)
        mean_pure_ratio2 = sum(pure_ratio_2_list) / len(pure_ratio_2_list)
        
        # Log results
        print('Epoch [%d/%d] Test Accuracy on the %s test images: Model1 %.4f %% Model2 %.4f %%, Pure Ratio 1 %.4f %%, Pure Ratio 2 %.4f %%' % (epoch+1, args.n_epoch, len(test_dataset), test_acc1, test_acc2, mean_pure_ratio1, mean_pure_ratio2))
        with open(txtfile, "a") as myfile:
            myfile.write(str(int(epoch)) + ': '  + str(train_acc1) +' '  + str(train_acc2) +' '  + str(test_acc1) + " " + str(test_acc2) + ' ' + str(mean_pure_ratio1) + ' ' + str(mean_pure_ratio2) + "\n")

if __name__=='__main__':
    main()
