""" The document module provides the Document class, which is a container
for all Bokeh objects that must be reflected to the client side BokehJS
library.

"""
from __future__ import absolute_import

import logging
logger = logging.getLogger(__file__)

from json import loads
import uuid

from six import string_types

from .model import Model
from .query import find
from .deprecate import deprecated
from .validation import check_integrity
from .util.callback_manager import _check_callback
from .util.version import __version__
from ._json_encoder import serialize_json
from .themes import default as default_theme
from .themes import Theme

DEFAULT_TITLE = "Bokeh Application"

class DocumentChangedEvent(object):
    def __init__(self, document):
        self.document = document

    def dispatch(self, receiver):
        if hasattr(receiver, '_document_changed'):
            receiver._document_changed(self)

class DocumentPatchedEvent(DocumentChangedEvent):
    def __init__(self, document):
        self.document = document

    def dispatch(self, receiver):
        super(DocumentPatchedEvent, self).dispatch(receiver)
        if hasattr(receiver, '_document_patched'):
            receiver._document_patched(self)

class ModelChangedEvent(DocumentPatchedEvent):
    def __init__(self, document, model, attr, old, new):
        super(ModelChangedEvent, self).__init__(document)
        self.model = model
        self.attr = attr
        self.old = old
        self.new = new

    def dispatch(self, receiver):
        super(ModelChangedEvent, self).dispatch(receiver)
        if hasattr(receiver, '_document_model_changed'):
            receiver._document_patched(self)

class TitleChangedEvent(DocumentPatchedEvent):
    def __init__(self, document, title):
        super(TitleChangedEvent, self).__init__(document)
        self.title = title

class RootAddedEvent(DocumentPatchedEvent):
    def __init__(self, document, model):
        super(RootAddedEvent, self).__init__(document)
        self.model = model

class RootRemovedEvent(DocumentPatchedEvent):
    def __init__(self, document, model):
        super(RootRemovedEvent, self).__init__(document)
        self.model = model

class SessionCallbackAdded(DocumentChangedEvent):
    def __init__(self, document, callback):
        super(SessionCallbackAdded, self).__init__(document)
        self.callback = callback

    def dispatch(self, receiver):
        super(SessionCallbackAdded, self).dispatch(receiver)
        if hasattr(receiver, '_session_callback_added'):
            receiver._session_callback_added(self)

class SessionCallbackRemoved(DocumentChangedEvent):
    def __init__(self, document, callback):
        super(SessionCallbackRemoved, self).__init__(document)
        self.callback = callback

    def dispatch(self, receiver):
        super(SessionCallbackRemoved, self).dispatch(receiver)
        if hasattr(receiver, '_session_callback_removed'):
            receiver._session_callback_removed(self)

class SessionCallback(object):
    def __init__(self, document, callback, id=None):
        if id is None:
            self._id = str(uuid.uuid4())
        else:
            self._id = id

        self._document = document
        self._callback = callback

    @property
    def id(self):
        return self._id

    @property
    def callback(self):
        return self._callback

    def remove(self):
        self.document._remove_session_callback(self)

class PeriodicCallback(SessionCallback):
    def __init__(self, document, callback, period, id=None):
        super(PeriodicCallback, self).__init__(document, callback, id)
        self._period = period

    @property
    def period(self):
        return self._period

class TimeoutCallback(SessionCallback):
    def __init__(self, document, callback, timeout, id=None):
        super(TimeoutCallback, self).__init__(document, callback, id)
        self._timeout = timeout

    @property
    def timeout(self):
        return self._timeout

class _MultiValuedDict(object):
    """
    This is to store a mapping from keys to multiple values, while avoiding
    the overhead of always having a collection as the value.
    """
    def __init__(self):
        self._dict = dict()

    def add_value(self, key, value):
        if key is None:
            raise ValueError("Key is None")
        if value is None:
            raise ValueError("Can't put None in this dict")
        if isinstance(value, set):
            raise ValueError("Can't put sets in this dict")
        existing = self._dict.get(key, None)
        if existing is None:
            self._dict[key] = value
        elif isinstance(existing, set):
            existing.add(value)
        else:
            self._dict[key] = set([existing, value])

    def remove_value(self, key, value):
        if key is None:
            raise ValueError("Key is None")
        existing = self._dict.get(key, None)
        if isinstance(existing, set):
            existing.discard(value)
            if len(existing) == 0:
                del self._dict[key]
        elif existing == value:
            del self._dict[key]
        else:
            pass

    def get_one(self, k, duplicate_error):
        existing = self._dict.get(k, None)
        if isinstance(existing, set):
            if len(existing) == 1:
                return next(iter(existing))
            else:
                raise ValueError(duplicate_error + (": %r" % (existing)))
        else:
            return existing

    def get_all(self, k):
        existing = self._dict.get(k, None)
        if existing is None:
            return []
        elif isinstance(existing, set):
            return list(existing)
        else:
            return [existing]

class Document(object):

    def __init__(self, **kwargs):
        self._roots = set()
        self._theme = kwargs.pop('theme', default_theme)
        # use _title directly because we don't need to trigger an event
        self._title = kwargs.pop('title', DEFAULT_TITLE)

        # TODO (bev) add vars, stores

        self._all_models_freeze_count = 0
        self._all_models = dict()
        self._all_models_by_name = _MultiValuedDict()
        self._callbacks = {}
        self._session_callbacks = {}

    def clear(self):
        ''' Remove all content from the document (including roots, vars, stores) but do not reset title'''
        self._push_all_models_freeze()
        try:
            while len(self._roots) > 0:
                r = next(iter(self._roots))
                self.remove_root(r)
        finally:
            self._pop_all_models_freeze()

    def _destructively_move(self, dest_doc):
        '''Move all fields in this doc to the dest_doc, leaving this doc empty'''
        if dest_doc is self:
            raise RuntimeError("Attempted to overwrite a document with itself")
        dest_doc.clear()
        # we have to remove ALL roots before adding any
        # to the new doc or else models referenced from multiple
        # roots could be in both docs at once, which isn't allowed.
        roots = []
        self._push_all_models_freeze()
        try:
            while self.roots:
                r = next(iter(self.roots))
                self.remove_root(r)
                roots.append(r)
        finally:
            self._pop_all_models_freeze()
        for r in roots:
            if r.document is not None:
                raise RuntimeError("Somehow we didn't detach %r" % (r))
        if len(self._all_models) != 0:
            raise RuntimeError("_all_models still had stuff in it: %r" % (self._all_models))
        for r in roots:
            dest_doc.add_root(r)

        dest_doc.title = self.title

    def _push_all_models_freeze(self):
        self._all_models_freeze_count += 1

    def _pop_all_models_freeze(self):
        self._all_models_freeze_count -= 1
        if self._all_models_freeze_count == 0:
            self._recompute_all_models()

    def _invalidate_all_models(self):
        # if freeze count is > 0, we'll recompute on unfreeze
        if self._all_models_freeze_count == 0:
            self._recompute_all_models()

    def _recompute_all_models(self):
        new_all_models_set = set()
        for r in self.roots:
            new_all_models_set = new_all_models_set.union(r.references())
        old_all_models_set = set(self._all_models.values())
        to_detach = old_all_models_set - new_all_models_set
        to_attach = new_all_models_set - old_all_models_set
        recomputed = {}
        recomputed_by_name = _MultiValuedDict()
        for m in new_all_models_set:
            recomputed[m._id] = m
            if m.name is not None:
                recomputed_by_name.add_value(m.name, m)
        for d in to_detach:
            d._detach_document()
        for a in to_attach:
            a._attach_document(self)
        self._all_models = recomputed
        self._all_models_by_name = recomputed_by_name

    @property
    def roots(self):
        return set(self._roots)

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, title):
        if title is None:
            raise ValueError("Document title may not be None")
        if self._title != title:
            self._title = title
            self._trigger_on_change(TitleChangedEvent(self, title))

    @property
    def theme(self):
        """ Get the current Theme instance affecting models in this Document. Never returns None."""
        return self._theme

    @theme.setter
    def theme(self, theme):
        """ Set the current Theme instance affecting models in this Document.
        Setting this to None sets the default theme. Changing theme may trigger
        model change events on the models in the Document if the theme modifies
        any model properties.
        """
        if theme is None:
            theme = default_theme
        if not isinstance(theme, Theme):
            raise ValueError("Theme must be an instance of the Theme class")
        if self._theme is theme:
            return
        self._theme = theme
        for model in self._all_models.values():
            self._theme.apply_to_model(model)

    def add_root(self, model):
        ''' Add a model as a root model to this Document.

        Any changes to this model (including to other models referred to
        by it) will trigger "on_change" callbacks registered on this
        Document.

        '''
        if model in self._roots:
            return
        self._push_all_models_freeze()
        try:
            self._roots.add(model)
        finally:
            self._pop_all_models_freeze()
        self._trigger_on_change(RootAddedEvent(self, model))

    @deprecated("Bokeh 0.11.0", "document.add_root")
    def add(self, *objects):
        """ Call add_root() on each object.
        .. warning::
            This function should only be called on top level objects such
            as Plot, and Layout containers.
        Args:
            *objects (Model) : objects to add to the Document
        Returns:
            None
        """
        for obj in objects:
            self.add_root(obj)

    def remove_root(self, model):
        ''' Remove a model as root model from this Document.

        Changes to this model may still trigger "on_change" callbacks
        on this Document, if the model is still referred to by other
        root models.
        '''
        if model not in self._roots:
            return # TODO (bev) ValueError?
        self._push_all_models_freeze()
        try:
            self._roots.remove(model)
        finally:
            self._pop_all_models_freeze()
        self._trigger_on_change(RootRemovedEvent(self, model))

    def get_model_by_id(self, model_id):
        ''' Get the model object for the given ID or None if not found'''
        return self._all_models.get(model_id, None)

    def get_model_by_name(self, name):
        ''' Get the model object for the given name or None if not found'''
        return self._all_models_by_name.get_one(name, "Found more than one model named '%s'" % name)

    def _is_single_string_selector(self, selector, field):
        if len(selector) != 1:
            return False
        if field not in selector:
            return False
        return isinstance(selector[field], string_types)

    def select(self, selector):
        ''' Query this document for objects that match the given selector.

        Args:
            selector (JSON-like) :

        Returns:
            seq[Model]

        '''
        if self._is_single_string_selector(selector, 'name'):
            # special-case optimization for by-name query
            return self._all_models_by_name.get_all(selector['name'])
        else:
            return find(self._all_models.values(), selector)

    def select_one(self, selector):
        ''' Query this document for objects that match the given selector.
        Raises an error if more than one object is found.  Returns
        single matching object, or None if nothing is found

        Args:
            selector (JSON-like) :

        Returns:
            Model

        '''
        result = list(self.select(selector))
        if len(result) > 1:
            raise ValueError("Found more than one model matching %s: %r" % (selector, result))
        if len(result) == 0:
            return None
        return result[0]

    def set_select(self, selector, updates):
        ''' Update objects that match a given selector with the specified
        attribute/value updates.

        Args:
            selector (JSON-like) :
            updates (dict) :

        Returns:
            None

        '''
        for obj in self.select(selector):
            for key, val in updates.items():
                setattr(obj, key, val)

    def on_change(self, *callbacks):
        ''' Invoke callback if the document or any Model reachable from its roots changes.

        '''
        for callback in callbacks:

            if callback in self._callbacks: continue

            _check_callback(callback, ('event',))

            self._callbacks[callback] = callback

    def on_change_dispatch_to(self, receiver):
        if not receiver in self._callbacks:
            self._callbacks[receiver] = lambda event: event.dispatch(receiver)

    def remove_on_change(self, *callbacks):
        ''' Remove a callback added earlier with on_change()

            Throws an error if the callback wasn't added

        '''
        for callback in callbacks:
            del self._callbacks[callback]

    def _with_self_as_curdoc(self, f):
        from bokeh.io import set_curdoc, curdoc
        old_doc = curdoc()
        try:
            set_curdoc(self)
            f()
        finally:
            set_curdoc(old_doc)

    def _wrap_with_self_as_curdoc(self, f):
        doc = self
        def wrapper(*args, **kwargs):
            def invoke():
                f(*args, **kwargs)
            doc._with_self_as_curdoc(invoke)
        return wrapper

    def _trigger_on_change(self, event):
        def invoke_callbacks():
            for cb in self._callbacks.values():
                cb(event)
        self._with_self_as_curdoc(invoke_callbacks)

    def _notify_change(self, model, attr, old, new):
        ''' Called by Model when it changes
        '''
        # if name changes, update by-name index
        if attr == 'name':
            if old is not None:
                self._all_models_by_name.remove_value(old, model)
            if new is not None:
                self._all_models_by_name.add_value(new, model)

        self._trigger_on_change(ModelChangedEvent(self, model, attr, old, new))

    @classmethod
    def _references_json(cls, references):
        '''Given a list of all models in a graph, return JSON representing them and their properties.'''
        references_json = []
        for r in references:
            ref = r.ref
            ref['attributes'] = r._to_json_like(include_defaults=True)
            references_json.append(ref)

        return references_json

    @classmethod
    def _instantiate_references_json(cls, references_json):
        '''Given a JSON representation of all the models in a graph, return a dict of new model objects.'''

        # Create all instances, but without setting their props
        references = {}
        for obj in references_json:
            obj_id = obj['id']
            obj_type = obj.get('subtype', obj['type'])

            cls = Model.get_class(obj_type)
            instance = cls(id=obj_id, _block_events=True)
            if instance is None:
                raise RuntimeError('Error loading model from JSON (type: %s, id: %s)' % (obj_type, obj_id))
            references[instance._id] = instance

        return references

    @classmethod
    def _initialize_references_json(cls, references_json, references):
        '''Given a JSON representation of the models in a graph and new model objects, set the properties on the models from the JSON'''

        for obj in references_json:
            obj_id = obj['id']
            obj_attrs = obj['attributes']

            instance = references[obj_id]

            # replace references with actual instances in obj_attrs
            for p in instance.properties_with_refs():
                if p in obj_attrs:
                    prop = instance.lookup(p)
                    obj_attrs[p] = prop.from_json(obj_attrs[p], models=references)

            # set all properties on the instance
            remove = []
            for key in obj_attrs:
                if key not in instance.properties():
                    logger.warn("Client sent attr %r for instance %r, which is a client-only or invalid attribute that shouldn't have been sent", key, instance)
                    remove.append(key)
            for key in remove:
                del obj_attrs[key]
            instance.update(**obj_attrs)

    def to_json_string(self, indent=None):
        ''' Convert the document to a JSON string.

        Args:
            indent (int or None, optional) : number of spaces to indent, or
                None to suppress all newlines and indentation (default: None)

        Returns:
            str

        '''
        root_ids = []
        for r in self._roots:
            root_ids.append(r._id)

        root_references = self._all_models.values()

        json = {
            'title' : self.title,
            'roots' : {
                'root_ids' : root_ids,
                'references' : self._references_json(root_references)
            },
            'version' : __version__
        }

        return serialize_json(json, indent=indent, sort_keys=True)

    def to_json(self):
        ''' Convert the document to a JSON object. '''

        # this is a total hack to go via a string, needed because
        # our BokehJSONEncoder goes straight to a string.
        doc_json = self.to_json_string()

        return loads(doc_json)

    @classmethod
    def from_json_string(cls, json):
        ''' Load a document from JSON. '''
        json_parsed = loads(json)
        return cls.from_json(json_parsed)

    @classmethod
    def from_json(cls, json):
        ''' Load a document from JSON. '''
        roots_json = json['roots']
        root_ids = roots_json['root_ids']
        references_json = roots_json['references']

        references = cls._instantiate_references_json(references_json)
        cls._initialize_references_json(references_json, references)

        doc = Document()
        for r in root_ids:
            doc.add_root(references[r])

        doc.title = json['title']

        return doc

    def replace_with_json(self, json):
        ''' Overwrite everything in this document with the JSON-encoded document '''
        replacement = self.from_json(json)
        replacement._destructively_move(self)

    def create_json_patch_string(self, events):
        ''' Create a JSON string describing a patch to be applied with apply_json_patch_string()

            Args:
              events : list of events to be translated into patches

            Returns:
              str :  JSON string which can be applied to make the given updates to obj
        '''
        references = set()
        json_events = []
        for event in events:
            if event.document is not self:
                raise ValueError("Cannot create a patch using events from a different document " + repr(event))

            if isinstance(event, ModelChangedEvent):
                value = event.new

                # the new value is an object that may have
                # not-yet-in-the-remote-doc references, and may also
                # itself not be in the remote doc yet.  the remote may
                # already have some of the references, but
                # unfortunately we don't have an easy way to know
                # unless we were to check BEFORE the attr gets changed
                # (we need the old _all_models before setting the
                # property). So we have to send all the references the
                # remote could need, even though it could be inefficient.
                # If it turns out we need to fix this we could probably
                # do it by adding some complexity.
                value_refs = set(Model.collect_models(value))

                # we know we don't want a whole new copy of the obj we're patching
                # unless it's also the new value
                if event.model != value:
                    value_refs.discard(event.model)
                references = references.union(value_refs)

                json_events.append({ 'kind' : 'ModelChanged',
                                     'model' : event.model.ref,
                                     'attr' : event.attr,
                                     'new' : value })
            elif isinstance(event, RootAddedEvent):
                references = references.union(event.model.references())
                json_events.append({ 'kind' : 'RootAdded',
                                     'model' : event.model.ref })
            elif isinstance(event, RootRemovedEvent):
                json_events.append({ 'kind' : 'RootRemoved',
                                     'model' : event.model.ref })
            elif isinstance(event, TitleChangedEvent):
                json_events.append({ 'kind' : 'TitleChanged',
                                     'title' : event.title })

        json = {
            'events' : json_events,
            'references' : self._references_json(references)
            }

        return serialize_json(json)

    def apply_json_patch_string(self, patch):
        ''' Apply a JSON patch string created by create_json_patch_string() '''
        json_parsed = loads(patch)
        self.apply_json_patch(json_parsed)

    def apply_json_patch(self, patch):
        ''' Apply a JSON patch object created by parsing the result of create_json_patch_string() '''
        references_json = patch['references']
        events_json = patch['events']
        references = self._instantiate_references_json(references_json)

        # Use our existing model instances whenever we have them
        for obj in references.values():
            if obj._id in self._all_models:
                references[obj._id] = self._all_models[obj._id]

        # The model being changed isn't always in references so add it in
        for event_json in events_json:
            if 'model' in event_json:
                model_id = event_json['model']['id']
                if model_id in self._all_models:
                    references[model_id] = self._all_models[model_id]

        self._initialize_references_json(references_json, references)

        for event_json in events_json:
            if event_json['kind'] == 'ModelChanged':
                patched_id = event_json['model']['id']
                if patched_id not in self._all_models:
                    raise RuntimeError("Cannot apply patch to %s which is not in the document" % (str(patched_id)))
                patched_obj = self._all_models[patched_id]
                attr = event_json['attr']
                value = event_json['new']
                if attr in patched_obj.properties_with_refs():
                    prop = patched_obj.lookup(attr)
                    value = prop.from_json(value, models=references)
                if attr in patched_obj.properties():
                    #logger.debug("Patching attribute %s of %r", attr, patched_obj)
                    patched_obj.update(** { attr : value })
                else:
                    logger.warn("Client sent attr %r on obj %r, which is a client-only or invalid attribute that shouldn't have been sent", attr, patched_obj)
            elif event_json['kind'] == 'RootAdded':
                root_id = event_json['model']['id']
                root_obj = references[root_id]
                self.add_root(root_obj)
            elif event_json['kind'] == 'RootRemoved':
                root_id = event_json['model']['id']
                root_obj = references[root_id]
                self.remove_root(root_obj)
            elif event_json['kind'] == 'TitleChanged':
                self.title = event_json['title']
            else:
                raise RuntimeError("Unknown patch event " + repr(event_json))

    def validate(self):
        # logging.basicConfig is a no-op if there's already
        # some logging configured. We want to make sure warnings
        # go somewhere so configure here if nobody has.
        logging.basicConfig(level=logging.INFO)
        root_sets = []
        for r in self.roots:
            refs = r.references()
            root_sets.append(refs)
            check_integrity(refs)

    @property
    def session_callbacks(self):
        return list(self._session_callbacks.values())

    def add_periodic_callback(self, callback, period, id=None):
        ''' Add callback so it can be invoked on a session periodically accordingly to period.

        NOTE: periodic callbacks can only work within a session. It'll take no effect when bokeh output is html or notebook

        '''
        # create the new callback object
        cb = PeriodicCallback(self, self._wrap_with_self_as_curdoc(callback), period, id)
        self._session_callbacks[callback] = cb
        # emit event so the session is notified of the new callback
        self._trigger_on_change(SessionCallbackAdded(self, cb))
        return cb

    def remove_periodic_callback(self, callback):
        ''' Remove a callback added earlier with add_periodic_callback()

            Throws an error if the callback wasn't added

        '''
        self._remove_session_callback(callback)

    def add_timeout_callback(self, callback, timeout, id=None):
        ''' Add callback so it can be invoked on a session periodically accordingly to period.

        NOTE: periodic callbacks can only work within a session. It'll take no effect when bokeh output is html or notebook

        '''
        # create the new callback object
        cb = TimeoutCallback(self, self._wrap_with_self_as_curdoc(callback), timeout, id)
        self._session_callbacks[callback] = cb
        # emit event so the session is notified of the new callback
        self._trigger_on_change(SessionCallbackAdded(self, cb))
        return cb

    def remove_timeout_callback(self, callback):
        ''' Remove a callback added earlier with add_timeout_callback()

            Throws an error if the callback wasn't added

        '''
        self._remove_session_callback(callback)


    def _remove_session_callback(self, callback):
        ''' Remove a callback added earlier with add_periodic_callback()
        or add_timeout_callback()

            Throws an error if the callback wasn't added

        '''
        cb = self._session_callbacks.pop(callback)
        # emit event so the session is notified and can remove the callback
        self._trigger_on_change(SessionCallbackRemoved(self, cb))
