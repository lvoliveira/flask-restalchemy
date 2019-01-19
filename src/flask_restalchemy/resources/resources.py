import warnings

from flask import request, json, jsonify
from flask.views import MethodView
from sqlalchemy.orm import load_only
from sqlalchemy.orm.collections import InstrumentedList

from flask_restalchemy.serialization.modelserializer import ModelSerializer
from .querybuilder import query_from_request


class BaseResource(MethodView):

    def __init__(self, declarative_model, serializer, session_getter, request_decorators=None):
        """
        The Base class for ORM resources

        :param class declarative_model: the SQLAlchemy declarative class.

        :param ModelSchema serializer: Marshmallow schema for serialization. If `None`, a default serializer will be
            created.

        :param callable session_getter: a callable that returns the DB session. A callable is used since a reference to
            DB session may not be available on the resource initialization.
        """
        self._resource_model = declarative_model
        self._serializer = serializer
        self._serializer.strict = True
        assert isinstance(self._serializer,
                          ModelSerializer), 'Invalid serializer instance: {}'.format(serializer)
        self._session_getter = session_getter
        if request_decorators:
            for decorator in request_decorators:
                self.dispatch_request = decorator(self.dispatch_request)

    def dispatch_request(self, *args, **kwargs):
        view_response = super().dispatch_request(*args, **kwargs)
        data, code, header = unpack(view_response)
        return jsonify(data), code, header

    def save_from_request(self, extra_attrs={}):
        session = self._session_getter()
        model_obj = self._serializer.load(load_request_data())
        for attr_name, value in extra_attrs.items():
            setattr(model_obj, attr_name, value)
        session.add(model_obj)
        session.commit()
        return self._serializer.dump(model_obj).data

    def _save_model(self, model_obj, method):
        session = self._session_getter()
        session.add(model_obj)

        # run pre commit hooks
        if method == 'POST':
            self._serializer.before_post_commit(model_obj, session)
        elif method == 'PUT':
            self._serializer.before_put_commit(model_obj, session)

        session.commit()

        # run post commit hooks
        if method == 'POST':
            self._serializer.after_post_commit(model_obj, session)
        elif method == 'PUT':
            self._serializer.after_put_commit(model_obj, session)

    def _save_serialized(self, serialized_data, existing_model=None):
        model_obj = self._serializer.load(serialized_data, existing_model)

        method = 'PUT' if existing_model else 'POST'
        self._save_model(model_obj, method)

        return self._serializer.dump(model_obj)

    @property
    def _db_session(self):
        return self._session_getter()


class ModelResource(BaseResource):

    def get(self, id=None):
        if id is not None:
            data = self._resource_model.query.get(id)
            if data is None:
                return NOT_FOUND_ERROR, 404
            return self._serializer.dump(data)
        else:
            data = query_from_request(self._resource_model, self._serializer, request)
            return data

    def put(self, id):
        request_data = load_request_data()
        data = self._resource_model.query.get(id)
        if data is None:
            return NOT_FOUND_ERROR, 404

        serialized = self._serializer.dump(data)
        serialized.update(request_data)
        result = self._save_serialized(serialized, data)
        return result

    def delete(self, id):
        data = self._resource_model.query.get(id)
        if data is None:
            return NOT_FOUND_ERROR, 404
        session = self._db_session
        session.delete(data)
        was_deleted = len(session.deleted) > 0
        session.flush()
        session.commit()
        return '', 204

    def post(self):
        document = load_request_data()
        saved = self._save_serialized(document)
        return saved, 201


class ToManyRelationResource(BaseResource):
    """
    flask-restful resource class that receives two SQLAlchemy models, a parent model and a child model,
    and define the API to provide LIST and CREATE over data of the child model associated with a specific
    element of the parent model.
    """

    def __init__(self, relation_property, serializer, session_getter, request_decorators=None):
        """
        The Base class for ORM resources

        :param class declarative_model: the SQLAlchemy declarative class.

        :param ModelSchema serializer: Marshmallow schema for serialization. If `None`, a default serializer will be
            created.

        :param callable session_getter: a callable that returns the DB session. A callable is used since a reference to
            DB session may not be available on the resource initialization.
        """
        resource_model = relation_property.prop.mapper.class_
        super(ToManyRelationResource, self).__init__(resource_model, serializer, session_getter,
                                                     request_decorators)
        self._relation_property = relation_property
        self._related_model = relation_property.class_

    def get(self, relation_id, id=None):
        if id:
            requested_obj = self._query_related_obj(relation_id, id)
            if not requested_obj:
                return NOT_FOUND_ERROR, 404
            return self._serializer.dump(requested_obj), 200
        else:
            session = self._db_session
            # using options(load_only('id')) avoid unintended subquerying, as all we want is
            # check if the element exists
            related_obj = session.query(self._related_model).options(load_only("id")).get(
                relation_id)
            if related_obj is None:
                return NOT_FOUND_ERROR, 404

            # TODO: Is there a more efficient way than using getattr?
            relation_list_or_query = getattr(related_obj, self._relation_property.key)
            if isinstance(relation_list_or_query, InstrumentedList) or not hasattr(
                    relation_list_or_query, 'paginate'):
                warnings.warn(
                    'Warnning: relationship does not support pagination nor filter.'
                    'Use flask-sqlalchemy relationship with lazy="dynamic".')
                collection = [self._serializer.dump(item) for item in relation_list_or_query]
            else:
                collection = query_from_request(self._resource_model, self._serializer, request,
                                                query=relation_list_or_query)
            return collection

    def post(self, relation_id):
        session = self._db_session
        related_obj = session.query(self._related_model).get(relation_id)
        if not related_obj:
            return NOT_FOUND_ERROR, 404
        collection = getattr(related_obj, self._relation_property.key)
        data_dict = load_request_data()
        resource_id = data_dict.get('id', None)

        if resource_id is not None:
            return self.append_existent(collection, resource_id, session)

        new_obj = self._serializer.load(data_dict)
        session.add(new_obj)
        collection.append(new_obj)

        self._save_model(new_obj, 'POST')
        saved = self._serializer.dump(new_obj)
        return saved, 201

    def append_existent(self, collection, resource_id, session):
        resource_obj = session.query(self._resource_model).get(resource_id)
        if resource_obj is None:
            return NOT_FOUND_ERROR, 404
        collection.append(resource_obj)
        session.commit()
        return self._serializer.dump(resource_obj), 200

    def put(self, relation_id, id):
        request_data = load_request_data()
        requested_obj = self._query_related_obj(relation_id, id)
        if not requested_obj:
            return NOT_FOUND_ERROR, 404
        serialized = self._serializer.dump(requested_obj)
        serialized.update(request_data)
        saved = self._save_serialized(serialized, requested_obj)
        return saved

    def delete(self, relation_id, id):
        requested_obj = self._query_related_obj(relation_id, id)
        if not requested_obj:
            return NOT_FOUND_ERROR, 404
        session = self._db_session
        session.delete(requested_obj)
        was_deleted = len(session.deleted) > 0
        session.flush()
        session.commit()
        return '', 204

    def _query_related_obj(self, relation_id, id):
        """
        Query resource model by ID but also add the relationship as a query constrain.

        :param relation_id: id of the related model
        :param id: id of the model being required
        :return: model with 'id' that has a related model with 'related_id'
        """

        # This checks if there is a parent with the related child on its relation property
        related = self._db_session.query(self._related_model).options(load_only("id")).filter(
            self._related_model.id == relation_id, self._relation_property.any(id=id)).one_or_none()

        if related is None:
            return None

        return self._db_session.query(self._resource_model).get(id)


class CollectionPropertyResource(ToManyRelationResource):

    def __init__(self, declarative_model, related_model, property_name, serializer,
                 session_getter, request_decorators=None):
        super(ToManyRelationResource, self).__init__(declarative_model, serializer,
                                                     session_getter, request_decorators)
        self._related_model = related_model
        self._property_name = property_name

    def get(self, relation_id, id=None):
        session = self._db_session
        related_obj = session.query(self._related_model).get(relation_id)
        if related_obj is None:
            return NOT_FOUND_ERROR, 404
        relation_list_or_query = getattr(related_obj, self._property_name)
        if isinstance(relation_list_or_query, InstrumentedList) or not hasattr(
                relation_list_or_query, 'paginate'):
            warnings.warn(
                'Warnning: property ' + self._property_name + ' does not support pagination nor filter.'
                                                              ' Use flask-sqlalchemy and make your property return a query object')
            collection = [self._serializer.dump(item) for item in relation_list_or_query]
        else:
            collection = query_from_request(self._resource_model, self._serializer, request,
                                            query=relation_list_or_query)
        return collection

    def post(self, relation_id):
        return 'POST not allowed for property resources', 405


def load_request_data():
    """
    Returns request data as dict.

    :rtype: dict
    """
    if request.data:
        return json.loads(request.data.decode('utf-8'))
    else:
        return request.form.to_dict()


def unpack(value):
    """Return a three tuple of data, code, and headers"""
    if not isinstance(value, tuple):
        return value, 200, {}

    try:
        data, code, headers = value
        return data, code, headers
    except ValueError:
        pass

    try:
        data, code = value
        return data, code, {}
    except ValueError:
        pass

    return value, 200, {}


NOT_FOUND_ERROR = 'Resource not found in the database!'