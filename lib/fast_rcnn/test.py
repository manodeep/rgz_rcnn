# Modified by Chen Wu (chen.wu@icrar.org)

from __future__ import absolute_import
from __future__ import print_function
from fast_rcnn.config import cfg, get_output_dir
import argparse
from utils.timer import Timer
import numpy as np
import cv2
from utils.cython_nms import nms, nms_new
from utils.boxes_grid import get_boxes_grid
from utils.project_bbox import project_bbox_inv
import six.moves.cPickle
import heapq
from utils.blob import im_list_to_blob
import os
import math
from rpn_msr.generate import imdb_proposals_det
import tensorflow as tf
from fast_rcnn.bbox_transform import clip_boxes, bbox_transform_inv, bbox_contains
from six.moves import range
try:
    import matplotlib.pyplot as plt
except:
    print('Cannot run vis during test due to the unavailability of matplotlib')
from tensorflow.python.client import timeline
import time
from collections import defaultdict

def _get_image_blob(im):
    """Converts an image into a network input.
    Arguments:
        im (ndarray): a color image in BGR order
    Returns:
        blob (ndarray): a data blob holding an image pyramid
        im_scale_factors (list): list of image scales (relative to im) used
            in the image pyramid
    """
    im_orig = im.astype(np.float32, copy=True)
    im_orig -= cfg.PIXEL_MEANS

    im_shape = im_orig.shape
    im_size_min = np.min(im_shape[0:2])
    im_size_max = np.max(im_shape[0:2])

    processed_ims = []
    im_scale_factors = []

    for target_size in cfg.TEST.SCALES:
        im_scale = float(target_size) / float(im_size_min)
        # Prevent the biggest axis from being more than MAX_SIZE
        if np.round(im_scale * im_size_max) > cfg.TEST.MAX_SIZE:
            im_scale = float(cfg.TEST.MAX_SIZE) / float(im_size_max)
        im = cv2.resize(im_orig, None, None, fx=im_scale, fy=im_scale,
                        interpolation=cv2.INTER_LINEAR)
        im_scale_factors.append(im_scale)
        processed_ims.append(im)

    # Create a blob to hold the input images
    blob = im_list_to_blob(processed_ims)

    return blob, np.array(im_scale_factors)

def _get_rois_blob(im_rois, im_scale_factors):
    """Converts RoIs into network inputs.
    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        im_scale_factors (list): scale factors as returned by _get_image_blob
    Returns:
        blob (ndarray): R x 5 matrix of RoIs in the image pyramid
    """
    rois, levels = _project_im_rois(im_rois, im_scale_factors)
    rois_blob = np.hstack((levels, rois))
    return rois_blob.astype(np.float32, copy=False)

def _project_im_rois(im_rois, scales):
    """Project image RoIs into the image pyramid built by _get_image_blob.
    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        scales (list): scale factors as returned by _get_image_blob
    Returns:
        rois (ndarray): R x 4 matrix of projected RoI coordinates
        levels (list): image pyramid levels used by each projected RoI
    """
    im_rois = im_rois.astype(np.float, copy=False)
    scales = np.array(scales)

    if len(scales) > 1:
        widths = im_rois[:, 2] - im_rois[:, 0] + 1
        heights = im_rois[:, 3] - im_rois[:, 1] + 1

        areas = widths * heights
        scaled_areas = areas[:, np.newaxis] * (scales[np.newaxis, :] ** 2)
        diff_areas = np.abs(scaled_areas - 224 * 224)
        levels = diff_areas.argmin(axis=1)[:, np.newaxis]
    else:
        levels = np.zeros((im_rois.shape[0], 1), dtype=np.int)

    rois = im_rois * scales[levels]

    return rois, levels

def _get_blobs(im, rois):
    """Convert an image and RoIs within that image into network inputs."""
    if cfg.TEST.HAS_RPN:
        blobs = {'data' : None, 'rois' : None}
        blobs['data'], im_scale_factors = _get_image_blob(im)
    else:
        blobs = {'data' : None, 'rois' : None}
        blobs['data'], im_scale_factors = _get_image_blob(im)
        if cfg.IS_MULTISCALE:
            if cfg.IS_EXTRAPOLATING:
                blobs['rois'] = _get_rois_blob(rois, cfg.TEST.SCALES)
            else:
                blobs['rois'] = _get_rois_blob(rois, cfg.TEST.SCALES_BASE)
        else:
            blobs['rois'] = _get_rois_blob(rois, cfg.TEST.SCALES_BASE)

    return blobs, im_scale_factors

def _clip_boxes(boxes, im_shape):
    """Clip boxes to image boundaries."""
    # x1 >= 0
    boxes[:, 0::4] = np.maximum(boxes[:, 0::4], 0)
    # y1 >= 0
    boxes[:, 1::4] = np.maximum(boxes[:, 1::4], 0)
    # x2 < im_shape[1]
    boxes[:, 2::4] = np.minimum(boxes[:, 2::4], im_shape[1] - 1)
    # y2 < im_shape[0]
    boxes[:, 3::4] = np.minimum(boxes[:, 3::4], im_shape[0] - 1)
    return boxes


def _rescale_boxes(boxes, inds, scales):
    """Rescale boxes according to image rescaling."""

    for i in range(boxes.shape[0]):
        boxes[i,:] = boxes[i,:] / scales[int(inds[i])]

    return boxes


def im_detect(sess, net, im, boxes=None, save_vis_dir=None,
              img_name='', include_rpn_score=False):
    """Detect object classes in an image given object proposals.
    Arguments:
        net (caffe.Net): Fast R-CNN network to use
        im (ndarray): color image to test (in BGR order)
        boxes (ndarray): R x 4 array of object proposals
    Returns:
        scores (ndarray): R x K array of object class scores (K includes
            background as object category 0)
        boxes (ndarray): R x (4*K) array of predicted bounding boxes
    """

    blobs, im_scales = _get_blobs(im, boxes)

    # When mapping from image ROIs to feature map ROIs, there's some aliasing
    # (some distinct image ROIs get mapped to the same feature ROI).
    # Here, we identify duplicate feature ROIs, so we only compute features
    # on the unique subset.
    if cfg.DEDUP_BOXES > 0 and not cfg.TEST.HAS_RPN:
        v = np.array([1, 1e3, 1e6, 1e9, 1e12])
        hashes = np.round(blobs['rois'] * cfg.DEDUP_BOXES).dot(v)
        _, index, inv_index = np.unique(hashes, return_index=True,
                                        return_inverse=True)
        blobs['rois'] = blobs['rois'][index, :]
        boxes = boxes[index, :]

    if cfg.TEST.HAS_RPN:
        im_blob = blobs['data']
        blobs['im_info'] = np.array(
            [[im_blob.shape[1], im_blob.shape[2], im_scales[0]]],
            dtype=np.float32)
    # forward pass
    if cfg.TEST.HAS_RPN:
        feed_dict={net.data: blobs['data'], net.im_info: blobs['im_info'], net.keep_prob: 1.0}
    else:
        feed_dict={net.data: blobs['data'], net.rois: blobs['rois'], net.keep_prob: 1.0}

    run_options = None
    run_metadata = None
    if cfg.TEST.DEBUG_TIMELINE:
        run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
        run_metadata = tf.RunMetadata()

    #theta_tensor = tf.get_default_graph().get_tensor_by_name('spt_trans_theta')
    cls_score, cls_prob, bbox_pred, rois = sess.run([net.get_output('cls_score'),
    net.get_output('cls_prob'), net.get_output('bbox_pred'), net.get_output('rois')],
                                                    feed_dict=feed_dict,
                                                    options=run_options,
                                                    run_metadata=run_metadata)

    if (save_vis_dir is not None and os.path.exists(save_vis_dir)):
        # first get the weights out
        with tf.variable_scope('conv5_3', reuse=True) as scope:
            conv5_3_weights = tf.get_variable("weights")

        conv5_3_weights_np, conv5_3_features, st_pool_features =\
        sess.run([conv5_3_weights, net.get_output('conv5_3'), net.get_output('pool_5')],
                  feed_dict=feed_dict,
                  options=run_options,
                  run_metadata=run_metadata)
        np.save(os.path.join(save_vis_dir, '%s_conv5_3_w.npy' % img_name), conv5_3_weights_np)
        np.save(os.path.join(save_vis_dir, '%s_conv5_3_f.npy' % img_name), conv5_3_features)
        np.save(os.path.join(save_vis_dir, '%s_st_pool_f.npy' % img_name), st_pool_features)


    if cfg.TEST.HAS_RPN:
        assert len(im_scales) == 1, "Only single-image batch implemented"
        boxes = rois[:, 1:5] / im_scales[0]


    if cfg.TEST.SVM:
        # use the raw scores before softmax under the assumption they
        # were trained as linear SVMs
        scores = cls_score
    else:
        # use softmax estimated probabilities
        scores = cls_prob

    if cfg.TEST.BBOX_REG:
        # Apply bounding-box regression deltas
        box_deltas = bbox_pred
        pred_boxes = bbox_transform_inv(boxes, box_deltas)
        #project_bbox_inv(pred_boxes, theta) # project spatially transformed box back
        pred_boxes = _clip_boxes(pred_boxes, im.shape)
    else:
        # Simply repeat the boxes, once for each class
        pred_boxes = np.tile(boxes, (1, scores.shape[1]))

    if cfg.DEDUP_BOXES > 0 and not cfg.TEST.HAS_RPN:
        # Map scores and predictions back to the original set of boxes
        scores = scores[inv_index, :]
        pred_boxes = pred_boxes[inv_index, :]

    if cfg.TEST.DEBUG_TIMELINE:
        trace = timeline.Timeline(step_stats=run_metadata.step_stats)
        trace_file = open(str(int(time.time() * 1000)) + '-test-timeline.ctf.json', 'w')
        trace_file.write(trace.generate_chrome_trace_format(show_memory=False))
        trace_file.close()

    if (include_rpn_score):
        # score is a joint prob instead of conditional prob
        scores *= np.reshape(rois[:, 0], [-1, 1])
    return scores, pred_boxes


def vis_detections(im, class_name, dets, thresh=0.8):
    """Visual debugging of detections."""
    import matplotlib.pyplot as plt
    #im = im[:, :, (2, 1, 0)]
    for i in range(np.minimum(10, dets.shape[0])):
        bbox = dets[i, :4]
        score = dets[i, -1]
        if score > thresh:
            #plt.cla()
            #plt.imshow(im)
            plt.gca().add_patch(
                plt.Rectangle((bbox[0], bbox[1]),
                              bbox[2] - bbox[0],
                              bbox[3] - bbox[1], fill=False,
                              edgecolor='g', linewidth=3)
                )
            plt.gca().text(bbox[0], bbox[1] - 2,
                 '{:s} {:.3f}'.format(class_name, score),
                 bbox=dict(facecolor='blue', alpha=0.5),
                 fontsize=14, color='white')

            plt.title('{}  {:.3f}'.format(class_name, score))
    #plt.show()

def apply_nms(all_boxes, thresh):
    """Apply non-maximum suppression to all predicted boxes output by the
    test_net method.
    """
    num_classes = len(all_boxes)
    num_images = len(all_boxes[0])
    nms_boxes = [[[] for _ in range(num_images)]
                 for _ in range(num_classes)]
    for cls_ind in range(num_classes):
        for im_ind in range(num_images):
            dets = all_boxes[cls_ind][im_ind]
            if dets == []:
                continue

            x1 = dets[:, 0]
            y1 = dets[:, 1]
            x2 = dets[:, 2]
            y2 = dets[:, 3]
            scores = dets[:, 4]
            inds = np.where((x2 > x1) & (y2 > y1) & (scores > cfg.TEST.DET_THRESHOLD))[0]
            dets = dets[inds,:]
            if dets == []:
                continue

            keep = nms(dets, thresh)
            if len(keep) == 0:
                continue
            nms_boxes[cls_ind][im_ind] = dets[keep, :].copy()
    return nms_boxes

def remove_embedded(boxes, scores, remove_option=1):
    """
    Return indices of those that should be KEPT
    """
    removed_indices = set()
    num_props = boxes.shape[0]
    for i in range(num_props):
        if (i in removed_indices):
            continue
        bxA = boxes[i, :]
        for j in range(num_props):
            if ((j == i) or (j in removed_indices)):
                continue
            bxB = boxes[j, :]
            if (bbox_contains(bxA, bxB, delta=0)):
                if ((1 == remove_option) and (scores[i] != scores[j])):
                    if (scores[i] > scores[j]):
                        removed_indices.add(j)
                    else:
                        removed_indices.add(i)
                else: # remove_option == 2 or scores[i] == scores[j]
                    removed_indices.add(j)
    return sorted(set(range(num_props)) - removed_indices)
    # nr = len(removed_indices)
    # if (nr > 0):
    #     new_boxes = sorted(set(range(num_props)) - removed_indices)
    #     boxes = boxes[new_boxes, :]
    #     scores = scores[new_boxes]
    #
    # return boxes, scores

def test_net(sess, net, imdb, weights_filename , max_per_image=300,
             thresh=0.05, vis=False, force=False):
    """Test a Fast R-CNN network on an image database."""
    num_images = len(imdb.image_index)
    # all detections are collected into:
    #    all_boxes[cls][image] = N x 5 array of detections in
    #    (x1, y1, x2, y2, score)
    all_boxes = [[[] for _ in range(num_images)]
                 for _ in range(imdb.num_classes)]

    output_dir = get_output_dir(imdb, weights_filename)
    det_file = os.path.join(output_dir, 'detections.pkl')
    if (force and os.path.exists(det_file)):
        os.remove(det_file)
    if (not os.path.exists(det_file)):
        # timers
        _t = {'im_detect' : Timer(), 'misc' : Timer()}

        if not cfg.TEST.HAS_RPN:
            roidb = imdb.roidb

        for i in range(num_images):
            # filter out any ground truth boxes
            if cfg.TEST.HAS_RPN:
                box_proposals = None
            else:
                # The roidb may contain ground-truth rois (for example, if the roidb
                # comes from the training or val split). We only want to evaluate
                # detection on the *non*-ground-truth rois. We select those the rois
                # that have the gt_classes field set to 0, which means there's no
                # ground truth.
                box_proposals = roidb[i]['boxes'][roidb[i]['gt_classes'] == 0]

            im = cv2.imread(imdb.image_path_at(i))
            _t['im_detect'].tic()
            scores, boxes = im_detect(sess, net, im, box_proposals)
            _t['im_detect'].toc()

            _t['misc'].tic()
            if vis:
                image = im[:, :, (2, 1, 0)]
                plt.cla()
                plt.imshow(image)

            # skip j = 0, because it's the background class
            ttt = 0
            bbox_img = []
            bscore_img = []
            bbc = 0 #bbox count
            index_map = dict()
            for j in range(1, imdb.num_classes):
                inds = np.where(scores[:, j] > thresh)[0]
                ttt += len(inds)
                cls_scores = scores[inds, j]
                cls_boxes = boxes[inds, j*4:(j+1)*4]
                cls_dets = np.hstack((cls_boxes, cls_scores[:, np.newaxis])) \
                    .astype(np.float32, copy=False)
                keep = nms(cls_dets, cfg.TEST.NMS)
                cls_dets = cls_dets[keep, :]
                if vis:
                    vis_detections(image, imdb.classes[j], cls_dets)
                all_boxes[j][i] = cls_dets
                #cls_dets.shape == [nb_detections_for_cls_j, 5]
                # we need to get all bboxes in a image regardless of classes
                # if (cls_dets.shape[0] > 0):
                #     bbox_img.append(cls_dets[:, 0:-1])
                #     bscore_img.append(np.reshape(cls_dets[:, -1], [-1, 1]))
                #     # remember the mapping
                #     for bc in range(cls_dets.shape[0]):
                #         index_map[bbc] = (j, bc)
                #         bbc += 1
            removed = 0
            # if (len(bbox_img) > 0):
            #     boxes = np.vstack(bbox_img)
            #     scores = np.vstack(bscore_img)
            #     keep_indices = remove_embedded(boxes, scores, remove_option=1)
            #     removed = bbc - len(keep_indices)
            #     # need to find out which j, and which k correspond to which index
            #     cls_keep = defaultdict(list)
            #     for ki in keep_indices:
            #         j, bc = index_map[ki]
            #         cls_keep[j].append(bc)
            #
            #     for j in xrange(1, imdb.num_classes):
            #         if (j in cls_keep):
            #             all_boxes[j][i] = all_boxes[j][i][cls_keep[j], :]

            if vis:
               plt.show()
            # Limit to max_per_image detections *over all classes*
            if max_per_image > 0:
                image_scores = np.hstack([all_boxes[j][i][:, -1]
                                          for j in range(1, imdb.num_classes)])
                if len(image_scores) > max_per_image:
                    image_thresh = np.sort(image_scores)[-max_per_image]
                    for j in range(1, imdb.num_classes):
                        keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
                        all_boxes[j][i] = all_boxes[j][i][keep, :]
            _t['misc'].toc()

            print('im_detect: {:d}/{:d} {:d} detection {:d} removed {:.3f}s' \
                  .format(i + 1, num_images, ttt, removed, _t['im_detect'].average_time))

        with open(det_file, 'wb') as f:
            six.moves.cPickle.dump(all_boxes, f, six.moves.cPickle.HIGHEST_PROTOCOL)
    else:
        with open(det_file, 'r') as fin:
            all_boxes = six.moves.cPickle.load(fin)

    print('Evaluating detections')
    imdb.evaluate_detections(all_boxes, output_dir)
