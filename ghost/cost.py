import theano
import numpy as np
import theano.tensor as T
from theano.tensor.nlinalg import matrix_inverse, trace
from theano.tensor.nlinalg import det
from theano.sandbox.linalg import psd
from utils import print_with_stamp,gTrig2, gTrig_np, gTrig

def linear_loss(mx,Sx,params,absolute=True):
    # Quadratic penalty function
    Q = T.constant(params['Q'],dtype=mx.dtype)
    target = T.constant(params['target'],dtype=mx.dtype)
    delta = mx-target
    SxQ = Sx.dot(Q)
    m_cost = Q.T.dot(delta) 
    s_cost = Q.T.dot(Sx).dot(Q)

    return m_cost, s_cost

def quadratic_loss(mx,Sx,params,u=None, use_gTrig = True):
    # Quadratic penalty function
    if not 'Q' in params:
        Q = T.eye(Sx.shape[0])
    else:
         Q = T.constant(params['Q'],dtype=mx.dtype)
    target = T.constant(params['target'],dtype=mx.dtype)
    if 'angi' in params and use_gTrig:
        target, _, _= gTrig2(target, Sx, params['angi'], params['D'])
        mx, Sx, _ = gTrig2(mx, Sx, params['angi'], params['D'])
    delta = mx-target
    deltaQ = delta.T.dot(Q)
    SxQ = Sx.dot(Q)  
    if u is None:
        m_cost = T.sum(Sx*Q) + deltaQ.dot(delta)
    else:
        if not 'R' in params:
            R = T.zeros((u.shape[0],u.shape[0]))
            #R = T.eye(u.shape[0])
        else:
            R = T.constant(params['R'],dtype=mx.dtype)
        # m_cost = trace(Q.dot(Sx)) + deltaQ.dot(delta)
        # To whoever did the change in the previous line: 
        # The following line is equivalent to the trace of the matrix product. 
        # The trace is the sum of diagonal elements of a matrix, the diagonal elements
        # of a matrix product of two square matrices of the same dimensionallity correspond 
        # to the element wise multiplication between the two matrices.
        m_cost = T.sum(Sx*Q) + deltaQ.dot(delta)  
        m_cost = m_cost + T.transpose(u)*R*u
    s_cost = 2*T.sum(SxQ.dot(SxQ)) + 4*deltaQ.dot(Sx).dot(deltaQ.T)

    return m_cost, s_cost

def quadratic_saturating_loss(mx,Sx,params):
    # Quadratic penalty function
    Q = T.constant(params['Q'],dtype=mx.dtype)
    target = T.constant(params['target'],dtype=mx.dtype)
    delta = mx-target
    deltaQ = delta.T.dot(Q)
    SxQ = Sx.dot(Q)
    IpSxQ = T.eye(mx.shape[0]) + SxQ
    S1 = Q.dot(matrix_inverse(IpSxQ))
    m_cost = -T.exp (-0.5*delta.dot(S1).dot(delta))/T.sqrt(det(IpSxQ))
    Ip2SxQ = T.eye(mx.shape[0]) + 2*SxQ
    S2= Q.dot(matrix_inverse(Ip2SxQ))
    s_cost = T.exp (-delta.dot(S2).dot(delta))/T.sqrt(det(Ip2SxQ)) - m_cost**2

    return 1 + m_cost, s_cost

def generic_loss(mx,Sx,params,loss_func,angle_idims=[]):
    if len(angle_idims) > 0:
        mxa,Sxa = gTrig2(mx,Sx,angle_idims,mx.size)
    else:
        mxa = mx; Sxa = Sx
    return loss_func(mxa,Sxa,params)
