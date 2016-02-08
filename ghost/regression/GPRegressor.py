import os,sys
from functools import partial

import numpy as np
import theano
import theano.tensor as T
from scipy.optimize import minimize, basinhopping
from theano import function as F, shared as S
from theano.misc.pkl_utils import dump as t_dump, load as t_load
from theano.tensor.nlinalg import matrix_dot
from theano.sandbox.linalg import psd,matrix_inverse,det

import cov
import utils
from utils import gTrig, gTrig2,gTrig_np

class GP(object):
    def __init__(self, X_dataset, Y_dataset, name='GP', profile=False, angle_idims = [], angle_odims = [], uncertain_inputs=False, hyperparameter_gradients=False):
        # theano options
        self.profile= profile
        self.compile_mode = theano.compile.get_default_mode()#.excluding('scanOp_pushout_seqs_ops')

        # GP options
        self.min_method = "L-BFGS-B"
        self.state_changed = False
        self.uncertain_inputs = uncertain_inputs
        self.hyperparameter_gradients = hyperparameter_gradients
        
        # dimesnion related variables
        self.N = X_dataset.shape[0]
        self.D = X_dataset.shape[1]
        self.E = Y_dataset.shape[1]
        self.idims = self.D + len(angle_idims)
        self.odims = self.E + len(angle_odims)
        self.angle_idims = angle_idims
        self.angle_odims = angle_odims

        #symbolic varianbles
        self.X_ = None; self.Y_ = None
        self.X_shared = None; self.Y_shared = None; self.loghyp = None
        self.X = None; self.Y = None
        self.K = None; self.iK = None; self.beta = None;
        self.nlml = None

        # compiled functions
        self.predict_ = None
        self.predict_d_ = None

        # name of this class for printing command line output and saving
        self.name = name
        # filename for saving
        self.filename = '%s_%d_%d_%s_%s'%(self.name,self.idims,self.odims,theano.config.device,theano.config.floatX)
        if len(angle_idims) > 0:
            self.filename += '_angi'
            for i in xrange(len(angle_idims)):
                self.filename += str(angle_idims[i])
        if len(angle_odims) > 0:
            self.filename += '_ango'
            for i in xrange(len(angle_odims)):
                self.filename += str(angle_odims[i])

        try:
            # try loading from pickled file, to avoid recompiling
            self.load()
            if (X_dataset.shape[0] != self.X_.shape[0] or Y_dataset.shape[0] != self.Y_.shape[0]) or not( np.allclose(X_dataset,self.X_) and np.allclose(Y_dataset,self.Y_) ):
                self.set_dataset(X_dataset,Y_dataset)
        
        except IOError:
            # initialize the class if no pickled version is available
            assert X_dataset.shape[0] == Y_dataset.shape[0], "X_dataset and Y_dataset must have the same number of rows"
            self.set_dataset(X_dataset,Y_dataset)
            self.init_log_likelihood()
        
        utils.print_with_stamp('Finished initialising GP',self.name)
    
    def set_dataset(self,X_dataset,Y_dataset):
        utils.print_with_stamp('Updating GP dataset',self.name)
        # ensure we don't change the number of input and output dimensions ( the number of samples can change)
        if self.X_ is not None:
            assert self.X_.shape[1] == X_dataset.shape[1]
        if self.Y_ is not None:
            assert self.Y_.shape[1] == Y_dataset.shape[1]

        # first, assign the numpy arrays to class members
        if theano.config.floatX == 'float32':
            self.X_ = X_dataset.astype(np.float32)
            self.Y_ = Y_dataset.astype(np.float32)
        else:
            self.X_ = X_dataset
            self.Y_ = Y_dataset
        # dims = non_angle_dims + 2*angle_dims
        N = self.X_.shape[0]
        D = X_dataset.shape[1]
        E = Y_dataset.shape[1]
        idims = D + len(self.angle_idims)
        odims = E + len(self.angle_odims)
        self.N = N
        self.D = D
        self.E = E
        self.idims = idims
        self.odims = odims

        # Convert angle dimensions to complex representation. this ensures that we create separate hyperparameters 
        # for the 2 dimensions that correspond to a single angle input
        X_ = self.X_ if len(self.angle_idims) == 0 else gTrig_np(self.X_, self.angle_idims)
        Y_ = self.Y_ if len(self.angle_idims) == 0 else gTrig_np(self.Y_, self.angle_odims)
        
        # now we create symbolic shared variables ( borrowing the memory from the numpy arrays)
        if self.X_shared is None:
            self.X_shared = S(self.X_,name='X', borrow=True)
        else:
            self.X_shared.set_value(self.X_, borrow = True)
        if self.Y_shared is None:
            self.Y_shared = S(self.Y_,name='Y', borrow=True)
        else:
            self.Y_shared.set_value(self.Y_, borrow = True)

        # set the X and Y variables
        self.X = self.X_shared
        self.Y = self.Y_shared

        # convert angle dimensions to cartesian coordinates
        if len(self.angle_idims) > 0:
            utils.print_with_stamp('Converting angle dimensions in training dataset inputs to cartesian',self.name)
            self.X = gTrig(self.X_shared, self.angle_idims,D)

        if len(self.angle_odims) > 0:
            utils.print_with_stamp('Converting angle dimensions in training dataset outputs to cartesian',self.name)
            self.Y = gTrig(self.Y_shared, self.angle_odims, E)

        # if we have a compiled log likelihood function
        # initialize the loghyperparameters of the gp ( this code supports squared exponential only, at the moment)
        self.loghyp_ = np.zeros((odims,idims+2))
        if theano.config.floatX == 'float32':
            self.loghyp_ = self.loghyp_.astype(np.float32)
        self.loghyp_[:,:idims] = X_.std(0,ddof=1)
        self.loghyp_[:,idims] = Y_.std(0,ddof=1)
        self.loghyp_[:,idims+1] = 0.1*self.loghyp_[:,idims]
        self.loghyp_ = np.log(self.loghyp_)

        # this creates one theano shared variable for the loghyperparameters of each output dimension, separately
        if self.loghyp is None:
            self.loghyp = [S(self.loghyp_[i,:],name='loghyp', borrow=True) for i in xrange(odims)]
        else:
            for i in xrange(odims):
                self.loghyp[i].set_value(self.loghyp_[i,:], borrow = True)

        # we should be saving, since we updated the trianing dataset
        self.state_changed = True

    def set_loghyp(self, loghyp):
        loghyp = loghyp.reshape(self.loghyp_.shape)
        if theano.config.floatX == 'float32':
            loghyp = loghyp.astype(np.float32)
        np.copyto(self.loghyp_,loghyp)

    def get_params(self, symbolic=True):
        if symbolic:
            retvars = [self.X,self.Y]
            for lh in self.loghyp:
                retvars.append(lh)
            return retvars
        else:
            retvars = [self.X_, self.Y_, self.loghyp_]
            return retvars

    def init_log_likelihood(self):
        utils.print_with_stamp('Initialising expression graph for log likelihood',self.name)
        idims = self.idims
        odims = self.odims

        self.kernel_func = [[]]*odims
        self.K = [[]]*odims
        self.iK = [[]]*odims
        self.beta = [[]]*odims
        nlml = [[]]*odims
        dnlml = [[]]*odims
        covs = (cov.SEard, cov.Noise)
        for i in xrange(odims):
            # initialise the (before compilation) kernel function
            loghyps = (self.loghyp[i][:idims+1],self.loghyp[i][idims+1])
            self.kernel_func[i] = partial(cov.Sum, loghyps, covs)

            # We initialise the kernel matrices (one for each output dimension)
            self.K[i] = self.kernel_func[i](self.X)
            self.K[i].name = 'K_%d'%(i)
            self.iK[i] = matrix_inverse(psd(self.K[i]))
            self.iK[i].name = 'iK_%d'%(i)
            self.beta[i] = self.iK[i].dot(self.Y[:,i])
            self.beta[i].name = 'beta_%d'%(i)

            # And finally, the negative log marginal likelihood ( again, one for each dimension; although we could share
            # the loghyperparameters across all output dimensions and train the GPs jointly)
            nlml[i] = 0.5*(self.Y[:,i].T.dot(self.beta[i]) + T.log(det(psd(self.K[i]))) + self.N*T.log(2*np.pi) )
            # Compute the gradients for each output dimension independently
            dnlml[i] = T.jacobian(nlml[i].flatten(),self.loghyp[i])

        nlml = T.stack(nlml)
        dnlml = T.stack(dnlml)
        # Compile the theano functions
        utils.print_with_stamp('Compiling log likelihood',self.name)
        self.nlml = F((),nlml,name='%s>nlml'%(self.name), profile=self.profile, mode=self.compile_mode)
        utils.print_with_stamp('Compiling jacobian of log likelihood',self.name)
        self.dnlml = F((),(nlml,dnlml),name='%s>dnlml'%(self.name), profile=self.profile, mode=self.compile_mode)
        self.state_changed = True # for saving
    
    def init_predict(self, derivs=False):
        utils.print_with_stamp('Initialising expression graph for prediction',self.name)
        # Note that this handles n_samples inputsa
        # initialize variable for input vector ( iniput mean in the ccase of unceratin inputs )
        x_mean=None; x_cov=None
        if theano.config.floatX == 'float32':
            x_mean = T.fmatrix('x')      # n_samples x idims
            # initialize variable for input covariance 
            if self.uncertain_inputs:
                x_cov = T.ftensor3('x_cov')  # n_samples x idims x idims
        else:
            x_mean = T.dmatrix('x')      # n_samples x idims
            # initialize variable for input covariance 
            if self.uncertain_inputs:
                x_cov = T.dtensor3('x_cov')  # n_samples x idims x idims
        
        # get prediction
        retvars = self.predict_symbolic(x_mean,x_cov)
        prediction = []
        for r in retvars:
            if r is not None:
                prediction.append(r)
        
        if not derivs:
            # compile prediction
            utils.print_with_stamp('Compiling mean and variance of prediction',self.name)
            input_vars = [x_mean] if x_cov is None else [x_mean,x_cov]
            self.predict_ = F(input_vars,prediction,name='%s>predict_'%(self.name), profile=self.profile, mode=self.compile_mode)
            self.state_changed = True # for saving
        else:
            # compute the derivatives wrt the input vector ( or input mean in the case of uncertain inputs)
            prediction_derivatives = list(prediction)
            for p in prediction:
                prediction_derivatives.append( T.jacobian( p.flatten(),x_mean ) )
            if self.uncertain_inputs:
                # compute the derivatives wrt the input covariance
                for p in prediction:
                    prediction_derivatives.append( T.jacobian( p.flatten(),x_cov ) )
            if self.hyperparameter_gradients:
                # compute the derivatives wrt the hyperparameters
                for p in prediction:
                    dpdlh =[]
                    for i in xrange(self.odims):
                        dpdlh.append( T.jacobian( p.flatten(), self.loghyp[i] ) )
                    dpdlh = T.stack(dpdlh)
                    prediction_derivatives.append(dpdlh)
            #prediction_derivatives contains  [p1, p2, ..., pn, dp1/dx_mean, dp2/dx_mean, ..., dpn/dx_mean, dp1/dx_cov, dp2/dx_cov, ..., dpn/dx_cov, dp1/dloghyp, dp2/dloghyp, ..., dpn/loghyp]
            utils.print_with_stamp('Compiling derivatives of mean and variance of prediction',self.name)
            self.predict_d_ = F(input_vars, prediction_derivatives, name='%s>predict_d_'%(self.name), profile=self.profile, mode=self.compile_mode)
            self.state_changed = True # for saving

    def predict_symbolic(self,x_mean,x_cov):
        odims = self.odims
        # convert angle dimensions to cartesian coordinates
        m = x_mean
        if len(self.angle_idims) > 0:
            utils.print_with_stamp('Converting input angle dimensions to cartesian',self.name)
            m = gTrig(x_mean, self.angle_idims, self.D)

        # compute the mean and variance for each output dimension
        mean = [[]]*odims
        variance = [[]]*odims
        for i in xrange(odims):
            k = self.kernel_func[i](m,self.X)
            mean[i] = k.dot(self.beta[i])
            variance[i] = self.kernel_func[i](m,all_pairs=False) - (k*(k.dot( self.iK[i] )) ).sum(axis=1) #TODO verify this computation

        # compile the prediction function
        M = T.stack(mean).T
        S = T.stack(variance).T
        S ,updts = theano.scan(fn=lambda Si: T.diag(Si),sequences=[S], strict=True)

        return M,S
    
    def predict(self,x_mean,x_cov = None):
        if self.predict_ is None:
            self.init_predict(derivs=False)
        # cast to float 32 if necessary
        if theano.config.floatX == 'float32':
            x_mean = x_mean.astype(np.float32)

        odims = self.odims
        idims = self.D

        if len(x_mean.shape) == 1:
            # convert to row vector
            x_mean = x_mean[None,:].reshape((1,idims))
        n = x_mean.shape[0]

        res = None
        if self.uncertain_inputs:
            if x_cov is None:
                x_cov = np.zeros((n,idims,idims))
            if theano.config.floatX == 'float32':
                x_cov = x_cov.astype(np.float32).reshape((n,idims,idims))

            if len(x_cov.shape) == 2:
                # convert to 3d array
                x_cov = x_cov[None,:,:].reshape((1,idims,idims))
            res = self.predict_(x_mean, x_cov)
        else:
            res = self.predict_(x_mean)
        return res

    def predict_d(self,x_mean,x_cov=None):
        if self.predict_d_ is None:
            self.init_predict(derivs=True)
        # cast to float 32 if necessary
        if theano.config.floatX == 'float32':
            x_mean = x_mean.astype(np.float32)

        odims = self.odims
        idims = self.D

        n = x_mean.shape[0]
        if len(x_mean.shape) == 1:
            # convert to row vector
            x_mean = x_mean[None,:].reshape((n,idims))

        res = None
        if self.name == 'GP':
            res = self.predict_d_(x_mean)
        else:
            if x_cov is None:
                x_cov = np.zeros((n,idims,idims))
            if theano.config.floatX == 'float32':
                x_cov = x_cov.astype(np.float32).reshape((n,idims,idims))
            res = self.predict_d_(x_mean, x_cov)
        return res
    
    def loss(self,loghyp):
        self.set_loghyp(loghyp)
        res = self.dnlml()
        nlml = np.array(res[0]).sum()
        dnlml = np.array(res[1]).flatten()
        # on a 64bit system, scipy optimize complains if we pass a 32 bit float
        res = (nlml.astype(np.float64),dnlml.astype(np.float64)) if theano.config.floatX == 'float32' else (nlml,dnlml)
        #utils.print_with_stamp('%s, %s'%(str(res[0]),str(np.exp(loghyp))),self.name,True)
        utils.print_with_stamp('%s'%(str(res[0])),self.name,True)
        return res

    def train(self):
        init_hyp = self.loghyp_.copy()
        utils.print_with_stamp('Current hyperparameters:',self.name)
        loghyp0 = self.loghyp_.copy()
        print (loghyp0)
        utils.print_with_stamp('nlml: %s'%(np.array(self.nlml())),self.name)
        try:
            opt_res = minimize(self.loss, loghyp0, jac=True, method=self.min_method, tol=1e-9, options={'maxiter': 300})
        except ValueError:
            opt_res = minimize(self.loss, loghyp0, jac=True, method='CG', tol=1e-9, options={'maxiter': 300})

        #opt_res = basinhopping(self.loss, self.loghyp_, niter=2, minimizer_kwargs = {'jac': True, 'method': self.min_method, 'tol': 1e-9, 'options': {'maxiter': 250}})
        print ''
        loghyp = opt_res.x.reshape(self.loghyp_.shape)
        self.state_changed = not np.allclose(init_hyp,loghyp,1e-6,1e-9)
        np.copyto(self.loghyp_,loghyp)
        utils.print_with_stamp('New hyperparameters:',self.name)
        print (self.loghyp_)
        utils.print_with_stamp('nlml: %s'%(np.array(self.nlml())),self.name)

    def load(self):
        with open(self.filename+'.zip','rb') as f:
            utils.print_with_stamp('Loading compiled GP with %d inputs and %d outputs'%(self.idims,self.odims),self.name)
            state = t_load(f)
            self.set_state(state)
        self.state_changed = False
    
    def save(self):
        sys.setrecursionlimit(100000)
        if self.state_changed:
            with open(self.filename+'.zip','wb') as f:
                utils.print_with_stamp('Saving compiled GP with %d inputs and %d outputs'%(self.idims,self.odims),self.name)
                t_dump(self.get_state(),f,2)
            self.state_changed = False

    def set_state(self,state):
        self.X_shared = state[0]
        self.Y_shared = state[1]
        self.X = state[2]
        self.Y = state[3]
        self.angle_idims = state[4]
        self.angle_odims = state[5]
        self.loghyp = state[6]
        self.K = state[7]
        self.iK = state[8]
        self.beta = state[9]
        self.set_dataset(state[10],state[11])
        self.set_loghyp(state[12])
        self.nlml = state[13]
        self.dnlml = state[14]
        self.predict_ = state[15]
        self.predict_d_ = state[16]

    def get_state(self):
        return (self.X_shared,self.Y_shared,self.X,self.Y,self.angle_idims,self.angle_odims,self.loghyp,self.K,self.iK,self.beta,self.X_,self.Y_,self.loghyp_,self.nlml,self.dnlml,self.predict_,self.predict_d_)

class GP_UI(GP):
    def __init__(self, X_dataset, Y_dataset, name = 'GP_UI', profile=False, angle_idims = [], angle_odims = [], hyperparameter_gradients=False):
        super(GP_UI, self).__init__(X_dataset,Y_dataset,name,profile,angle_idims,angle_odims,True,hyperparameter_gradients)

    def predict_symbolic(self,x_mean,x_cov):
        idims = self.idims
        odims = self.odims

        # convert angle dimensions to cartesian coordinates
        m = x_mean
        s = x_cov
        if len(self.angle_idims) > 0:
            utils.print_with_stamp('Converting input angle dimensions to cartesian',self.name)
            m, s = gTrig2(x_mean, x_cov, self.angle_idims, self.D)

        #centralize inputs 
        zeta = self.X[None,:,:] - m[:,None,:]
        
        # predictive mean and covariance (for each output dimension)
        M = [] # mean
        V = [] # inv(x_cov).dot(input_output_cov)
        M2 = [[]]*(odims**2) # second moment

        def M_helper(inp_k,B_k,sf2):
            t_k = inp_k.dot(matrix_inverse(B_k))
            c_k = sf2/T.sqrt(det(B_k))
            return (t_k,c_k)
            
        #predictive second moment ( only the lower triangular part, including the diagonal)
        def M2_helper(logk_i_k, logk_j_k, z_i_k, z_j_k, R_k, s_k):
            nk2 = logk_i_k[:,None] + logk_j_k[None,:] + utils.maha(z_i_k,-z_j_k,0.5*matrix_inverse(R_k).dot(s_k))
            tk = 1.0/T.sqrt(det(R_k))
            Qk = tk*T.exp( nk2 )
            return Qk

        logk=[[]]*odims
        Lambda=[[]]*odims
        z_=[[]]*odims
        for i in xrange(odims):
            # rescale input dimensions by inverse lengthscales
            iL = T.exp(-self.loghyp[i][:idims])
            inp = zeta*iL  # Note this is assuming a diagonal scaling matrix on the kernel

            # predictive mean ( which depends on input covariance )
            B = iL[:,None]*s*iL + T.eye(idims)
            (t,c), updts = theano.scan(fn=M_helper,sequences=[inp,B], non_sequences=[T.exp(2*self.loghyp[i][idims])], strict=True)
            l = T.exp(-0.5*T.sum(inp*t,2))
            lb = l*self.beta[i] # beta should have been precomputed in init_log_likelihood
            mean = T.sum(lb,1)*c;
            mean.name = 'M_%d'%(i)
            M.append(mean)

            # inv(s) times input output covariance (Eq 2.70)
            tiL = t*iL
            v = T.sum(tiL*lb[:,:,None],axis=1)*c[:,None]
            V.append(v)
            
            # predictive covariance
            logk[i] = 2*self.loghyp[i][idims] - 0.5*T.sum(inp*inp,2)
            Lambda[i] = T.exp(-2*self.loghyp[i][:idims])
            z_[i] = zeta*Lambda[i] 
            for j in xrange(i+1):
                # This comes from Deisenroth's thesis ( Eqs 2.51- 2.54 )
                R = s*(Lambda[i] + Lambda[j]) + T.eye(idims)
    
                Q,updts = theano.scan(fn=M2_helper, sequences=(logk[i],logk[j],z_[i],z_[j],R,s))
                Q.name = 'Q_%d%d'%(i,j)

                # Eq 2.55
                m2 = matrix_dot(self.beta[i], Q, self.beta[j].T)
                if i == j:
                    iKi = self.iK[i].dot(T.eye(self.N))
                    m2 =  m2 - T.sum(iKi*Q,(1,2)) + T.exp(2*self.loghyp[i][idims])
                else:
                    M2[j*odims+i] = m2
                m2.name = 'M2_%d%d'%(i,j)
                M2[i*odims+j] = m2

        M = T.stack(M).T
        V = T.stack(V).transpose(1,2,0)
        M2 = T.stack(M2).T
        S = M2.reshape((M.shape[0],odims,odims))
        S = S - (M[:,:,None]*M[:,None,:])

        return M,S,V
        
class SPGP(GP):
    def __init__(self, X_dataset, Y_dataset, name = 'SPGP',profile=False, n_inducing = 100, angle_idims = [], angle_odims = [], uncertain_inputs=False, hyperparameter_gradients=False):
        self.X_sp = None # inducing inputs
        self.Y_sp = None #inducing targets
        self.nlml_sp = None
        self.dnlml_sp = None
        self.kmeans = None
        self.n_inducing = n_inducing
        # intialize parent class params
        super(SPGP, self).__init__(X_dataset,Y_dataset,name,profile,angle_idims,angle_odims,uncertain_inputs,hyperparameter_gradients)

    def init_pseudo_inputs(self):
        if self.X_sp is None:
            utils.print_with_stamp('Compiling kmeans function',self.name)
            # pick initial cluster centers from dataset
            self.X_sp_ = np.random.multivariate_normal(self.X_.mean(0),np.diag(self.X_.var(0)),self.n_inducing)
            if theano.config.floatX == 'float32':
                self.X_sp_ = self.X_sp_.astype(np.float32)
            # this function corresponds to a single iteration of kmeans
            self.kmeans, self.X_sp, _ = utils.get_kmeans_func2(self.X, self.X_sp_)
        else:
            # pick initial cluster centers from dataset
            np.copyto(self.X_sp_,np.random.multivariate_normal(self.X_.mean(0),np.diag(self.X_.var(0)),self.n_inducing))

        utils.print_with_stamp('Initialising pseudo inputs',self.name)
        def kmeans_loss(X_sp):
            np.copyto(self.X_sp_,X_sp.reshape(self.X_sp_.shape))
            res = self.kmeans()
            # on a 64bit system, scipy optimize complains if we pass a 32 bit float
            res = (res[0].astype(np.float64),res[1].astype(np.float64))
            return res
        
        opt_res = minimize(kmeans_loss, self.X_sp_, jac=True, method=self.min_method, tol=1e-9, options={'maxiter': 500})
        X_sp = opt_res.x.reshape(self.X_sp_.shape)
        np.copyto(self.X_sp_,X_sp)
        # at this point X_sp_ should correspond to the kmeans centers of the dataset

    def set_dataset(self,X_dataset,Y_dataset):
        # set the dataset on the parent class
        super(SPGP, self).set_dataset(X_dataset,Y_dataset)
        if self.N > self.n_inducing:
            # init the shared variable for the pseudo inputs
            self.init_pseudo_inputs()
        
    def init_log_likelihood(self):
        # initialize the log likelihood of the GP class
        super(SPGP, self).init_log_likelihood()
        # here nlml and dnlml have already been innitialised, sow e can replace nlml and dnlml
        # only if we have enough data to train the pseudo inputs ( i.e. self.N > self.n_inducing)
        if self.N > self.n_inducing:
            odims = self.odims
            idims = self.idims
            # initialize the log likelihood of the sparse FITC approximation
            self.Kmm = [[]]*odims
            self.Bmm = [[]]*odims
            self.beta_sp = [[]]*odims
            nlml_sp = [[]]*odims
            dnlml_sp = [[]]*odims
            for i in xrange(odims):
                sf2 = T.exp(2*self.loghyp[i][idims])
                sn2 = T.exp(2*self.loghyp[i][idims+1])
                Kmm = self.kernel_func[i](self.X_sp)
                Kmn = self.kernel_func[i](self.X_sp,self.X)
                Qnn =  Kmn.T.dot((matrix_inverse(psd(Kmm))).dot(Kmn))

                # Gamma = diag(Knn - Qnn) + sn2*I
                Gamma = ((sf2 - T.diag(Qnn))/sn2 + 1)
                Gamma_inv = 1.0/Gamma
                # these operations are done to avoid inverting K_sp = (Qnn+Gamma)
                Kmn_ = Kmn*T.sqrt(Gamma_inv)                    # Kmn_*Gamma^-.5
                Yi = self.Y[:,i]*(T.sqrt(Gamma_inv))            # Gamma^-.5* Y
                Bmm = Kmm + (Kmn_).dot(Kmn_.T)                  # Kmm + Kmn * Gamma^-1 * Knm
                Bmn_ = matrix_inverse(psd(Bmm)).dot(Kmn_)       # (Kmm + Kmn * Gamma^-1 * Knm)^-1*Kmn*Gamma^-.5

                log_det_K_sp = T.sum(T.log(Gamma)) - T.log(det(psd(Kmm))) + T.log(det(psd(Bmm)))

                self.Kmm[i] = Kmm
                self.Bmm[i] = Bmm
                self.beta_sp[i] = Bmn_.dot(Yi)                  # (Kmm + Kmn * Gamma^-1 * Knm)^-1*Kmn*Gamma^-1*Y

                nlml_sp[i] = 0.5*( T.sum(Yi**2) - (Kmn_.dot(Yi)).T.dot(self.beta_sp[i]) + log_det_K_sp + self.N*T.log(2*np.pi) )/self.N
                # Compute the gradients for each output dimension independently wrt the hyperparameters AND the inducing input
                # locations Xb
                # TODO include the log hyperparameters in the optimization
                # TODO give the optiion for separate inducing inputs for every output dimension
                dnlml_sp[i] = (T.grad(nlml_sp[i],self.X_sp))

            nlml_sp = T.stack(nlml_sp)
            dnlml_sp = T.stack(dnlml_sp)
            # Compile the theano functions
            utils.print_with_stamp('Compiling FITC log likelihood',self.name)
            self.nlml_sp = F((),nlml_sp,name='%s>nlml_sp'%(self.name), profile=self.profile, mode=self.compile_mode)
            utils.print_with_stamp('Compiling jacobian of FITC log likelihood',self.name)
            self.dnlml_sp = F((),(nlml_sp,dnlml_sp),name='%s>dnlml_sp'%(self.name), profile=self.profile, mode=self.compile_mode)

    def predict_symbolic(self,x_mean,x_cov):
        if self.N < self.n_inducing:
            # stick with the full GP
            return super(SPGP, self).predict_symbolic()

        # convert angle dimensions to cartesian coordinates
        m = x_mean
        if len(self.angle_idims) > 0:
            utils.print_with_stamp('Converting input angle dimensions to cartesian',self.name)
            m = gTrig(x_mean, self.angle_idims, self.D)

        # compute the mean and variance for each output dimension
        mean = [[]]*odims
        variance = [[]]*odims
        for i in xrange(odims):
            k = self.kernel_func[i](m,self.X_sp)
            mean = k.dot(self.beta_sp[i])
            iK = matrix_inverse(psd(self.Kmm[i]))
            iB = matrix_inverse(psd(self.Bmm[i]))
            variance = self.kernel_func[i](m,all_pairs=False) - (k*(k.dot(iK) - k.dot(iB)) ).sum(axis=1)

        # compile the prediction function
        M = T.stack(mean).T
        S = T.stack(variance).T
        S ,updts = theano.scan(fn=lambda Si: T.diag(Si),sequences=[S], strict=True)

        return M,S

    def set_X_sp(self, X_sp):
        X_sp = X_sp.reshape(self.X_sp_.shape)
        if theano.config.floatX == 'float32':
            X_sp = X_sp.astype(np.float32)
        np.copyto(self.X_sp_,X_sp)

    def loss_sp(self,X_sp):
        self.set_X_sp(X_sp)
        res = self.dnlml_sp()
        nlml = np.array(res[0]).sum()
        dnlml = np.array(res[1]).flatten()
        # on a 64bit system, scipy optimize complains if we pass a 32 bit float
        res = (nlml.astype(np.float64),dnlml.astype(np.float64)) if theano.config.floatX == 'float32' else (nlml,dnlml)
        utils.print_with_stamp('%s'%(str(res[0])),self.name,True)
        return res

    def train(self):
        # train the full GP
        super(SPGP, self).train()

        # train the pseudo input locations
        utils.print_with_stamp('nlml SP: %s'%(np.array(self.nlml_sp())),self.name)
        opt_res = minimize(self.loss_sp, self.X_sp_, jac=True, method=self.min_method, tol=1e-9, options={'maxiter': 500})
        #opt_res = basinhopping(self.loss_sp, self.X_sp_, niter=2, minimizer_kwargs = {'jac': True, 'method': self.min_method, 'tol': 1e-9, 'options': {'maxiter': 250}})
        print ''
        X_sp = opt_res.x.reshape(self.X_sp_.shape)
        np.copyto(self.X_sp_,X_sp)
        utils.print_with_stamp('nlml SP: %s'%(np.array(self.nlml_sp())),self.name)

    def set_state(self,state):
        self.X = state[0]
        self.Y = state[1]
        self.loghyp = state[2]
        self.set_dataset(state[3],state[4])
        self.set_loghyp(state[5])
        self.nlml = state[6]
        self.dnlml = state[7]
        self.predict_ = state[8]
        self.predict_d_ = state[9]
        self.nlml_sp = state[10]
        self.dnlml_sp = state[11]
        self.kmeans = state[12]

    def get_state(self):
        return (self.X,self.Y,self.loghyp,self.X_,self.Y_,self.loghyp_,self.nlml,self.dnlml,self.predict_,self.predict_d_,self.nlml_sp,self.dnlml_sp,self.kmeans)

class SPGP_UI(SPGP,GP_UI):
    def __init__(self, X_dataset, Y_dataset, name = 'SPGP_UI',profile=False, n_inducing = 100, angle_idims = [], angle_odims = []):
        super(SPGP_UI, self).__init__(X_dataset,Y_dataset,name=name,profile=profile,n_inducing=n_inducing,angle_idims=angle_idims,angle_odims=angle_odims,uncertain_inputs=True)

    def predict_symbolic(self,x_mean,x_cov):
        if self.N < self.n_inducing:
            # stick with the full GP
            return GP_UI.predict_symbolic(self)

        idims = self.idims
        odims = self.odims

        # convert angle dimensions to cartesian coordinates
        m = x_mean
        s = x_cov
        if len(self.angle_idims) > 0:
            utils.print_with_stamp('Converting input angle dimensions to cartesian',self.name)
            m, s = gTrig2(x_mean, x_cov, self.angle_idims, self.D)

        #centralize inputs 
        zeta = self.X_sp[None,:,:] - m[:,None,:]
        
        # predictive mean and covariance (for each output dimension)
        M = [] # mean
        V = [] # inv(x_cov).dot(input_output_cov)
        M2 = [[]]*(odims**2) # second moment

        def M_helper(inp_k,B_k,sf2):
            t_k = inp_k.dot(matrix_inverse(B_k))
            c_k = sf2/T.sqrt(det(B_k))
            return (t_k,c_k)
            
        #predictive second moment ( only the lower triangular part, including the diagonal)
        def M2_helper(logk_i_k, logk_j_k, z_ij_k, R_k, s_k):
            nk2 = logk_i_k[:,None] + logk_j_k[None,:] + utils.maha(z_i_k,-z_j_k,0.5*matrix_inverse(R_k).dot(s_k))
            tk = 1.0/T.sqrt(det(R_k))
            Qk = T.exp( nk2 )
            return Qk,tk

        logk=[[]]*odims
        Lambda=[[]]*odims
        z_=[[]]*odims
        for i in xrange(odims):
            # rescale input dimensions by inverse lengthscales
            iL = T.exp(-self.loghyp[i][:idims])
            inp = zeta*iL  # Note this is assuming a diagonal scaling matrix on the kernel

            # predictive mean ( which depends on input covariance )
            B = iL[:,None]*s*iL + T.eye(idims)
            (t,c), updts = theano.scan(fn=M_helper,sequences=[inp,B], non_sequences=[T.exp(2*self.loghyp[i][idims])], strict=True)
            l = T.exp(-0.5*T.sum(inp*t,2))
            lb = l*self.beta_sp[i] # beta should have been precomputed in init_log_likelihood
            mean = T.sum(lb,1)*c;
            mean.name = 'M_%d'%(i)
            M.append(mean)

            # inv(x_cov) times input output covariance (Eq 2.70)
            tiL = t*iL
            v = T.sum(tiL*lb[:,:,None],axis=1)*c[:,None]
            V.append(v)
            
            # predictive covariance
            logk[i] = 2*self.loghyp[i][idims] - 0.5*T.sum(inp*inp,2)
            Lambda[i] = T.exp(-2*self.loghyp[i][:idims])
            z_[i] = zeta*Lambda[i] 
            for j in xrange(i+1):
                # This comes from Deisenroth's thesis ( Eqs 2.51- 2.54 )
                R = s*(Lambda[i] + Lambda[j]) + T.eye(idims)
    
                (Q,t),updts = theano.scan(fn=M2_helper, sequences=(logk[i],logk[j],z_[i],z_[j],R,s))
                Q.name = 'Q_%d%d'%(i,j)

                # Eq 2.55
                m2 = self.beta[i].dot(Q).dot(self.beta[j].T)
                if i == j:
                    iKi = matrix_inverse(psd(self.Kmm[i])).dot(T.eye(self.n_inducing)) - matrix_inverse(psd(self.Bmm[i])).dot(T.eye(self.n_inducing))
                    m2 =  t*(m2 - T.sum(iKi*Q,(1,2))) + T.exp(2*self.loghyp[i][idims])
                else:
                    m2 = t*m2
                    M2[j*odims+i] = m2
                m2.name = 'M2_%d%d'%(i,j)
                M2[i*odims+j] = m2

        M = T.stack(M).T
        V = T.stack(V).transpose(1,2,0)
        M2 = T.stack(M2).T
        S ,updts = theano.scan(fn=lambda M2i: M2i.reshape((odims,odims)), sequences=[M2], strict=True)
        S = S - (M[:,:,None]*M[:,None,:])
        
        return M,S,V

# RBF network (GP with uncertain inputs/deterministic outputs)
class RBFGP(GP_UI):
    def __init__(self, X_dataset, Y_dataset, sat_func=None, name = 'RBFGP',profile=False, angle_idims = [], angle_odims = []):
        self.sat_func = sat_func
        if self.sat_func is not None:
            name += '_sat'
        super(RBFGP, self).__init__(X_dataset,Y_dataset,name=name,profile=profile,angle_idims=angle_idims,angle_odims=angle_odims,hyperparameter_gradients=True)

    def predict_symbolic(self,x_mean,x_cov):
        idims = self.idims
        odims = self.odims

        # convert angle dimensions to cartesian coordinates
        m = x_mean
        s = x_cov
        if len(self.angle_idims) > 0:
            utils.print_with_stamp('Converting input angle dimensions to cartesian',self.name)
            m, s = gTrig2(x_mean, x_cov, self.angle_idims, self.D)

        #centralize inputs 
        zeta = self.X[None,:,:] - m[:,None,:]
        
        # predictive mean and covariance (for each output dimension)
        M = [] # mean
        V = [] # inv(x_cov).dot(input_output_cov)
        M2 = [[]]*(odims**2) # second moment

        def M_helper(inp_k,B_k,sf2):
            t_k = inp_k.dot(matrix_inverse(B_k))
            c_k = sf2/T.sqrt(det(B_k))
            return (t_k,c_k)
            
        #predictive second moment ( only the lower triangular part, including the diagonal)
        def M2_helper(logk_i_k, logk_j_k, z_i_k, z_j_k, R_k, s_k):
            nk2 = logk_i_k[:,None] + logk_j_k[None,:] + utils.maha(z_i_k,-z_j_k,0.5*matrix_inverse(R_k).dot(s_k))
            tk = 1.0/T.sqrt(det(R_k))
            Qk = tk*T.exp( nk2 )
            
            return Qk

        logk=[[]]*odims
        Lambda=[[]]*odims
        z_=[[]]*odims
        for i in xrange(odims):
            # rescale input dimensions by inverse lengthscales
            iL = T.exp(-self.loghyp[i][:idims])
            inp = zeta*iL  # Note this is assuming a diagonal scaling matrix on the kernel

            # predictive mean ( which depends on input covariance )
            B = iL[:,None]*s*iL + T.eye(idims)
            (t,c), updts = theano.scan(fn=M_helper,sequences=[inp,B], non_sequences=[T.exp(2*self.loghyp[i][idims])], strict=True)
            l = T.exp(-0.5*T.sum(inp*t,2))
            lb = l*self.beta[i] # beta should have been precomputed in init_log_likelihood
            mean = T.sum(lb,1)*c;
            mean.name = 'M_%d'%(i)
            M.append(mean)

            # inv(x_cov) times input output covariance (Eq 2.70)
            tiL = t*iL
            v = T.sum(tiL*lb[:,:,None],axis=1)*c[:,None]
            V.append(v)
            
            # predictive covariance
            logk[i] = 2*self.loghyp[i][idims] - 0.5*T.sum(inp*inp,2)
            Lambda[i] = T.exp(-2*self.loghyp[i][:idims])
            z_[i] = zeta*Lambda[i] 
            for j in xrange(i+1):
                # This comes from Deisenroth's thesis ( Eqs 2.51- 2.54 )
                R = s*(Lambda[i] + Lambda[j]) + T.eye(idims)
    
                Q,updts = theano.scan(fn=M2_helper, sequences=(logk[i],logk[j],z_[i],z_[j],R,s))
                Q.name = 'Q_%d%d'%(i,j)

                # Eq 2.55
                m2 = matrix_dot(self.beta[i],Q,self.beta[j].T)
                m2.name = 'M2_%d%d'%(i,j)
                if i == j:
                    m2 = m2 + 1e-6
                else:
                    M2[j*odims+i] = m2
                M2[i*odims+j] = m2

        M = T.stack(M).T
        V = T.stack(V).transpose(1,2,0) # TODO if using angle dimensions, this will give the input-ouput covariances with the augmented inputs
        M2 = T.stack(M2).T
        S = M2.reshape((M.shape[0],odims,odims))
        S = S - (M[:,:,None]*M[:,None,:])

        # apply saturating function to the output if available
        if self.sat_func is not None:
            # compute the joint input output covariance
            M,S,U = self.sat_func(M,S)
            V = (V[:,:,None,:]*(U[:,None,:,:])).sum(3)

        return M,S,V
