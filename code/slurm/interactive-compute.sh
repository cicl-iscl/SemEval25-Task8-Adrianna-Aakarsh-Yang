#!/bin/bash

salloc --partition=single --ntasks=1 --cpus-per-task=4 --mem=32G --time=02:00:00

source ./env.sh