import logging
import datetime
import enum
import os.path
import inspect
import dateutil.parser
import copy
import pymongo
import pymongo.errors
from typing import List, Dict
from flask_restplus import fields as flask_restplus_fields, reqparse, inputs
from bson.objectid import ObjectId
from bson.errors import BSONError
import json

from pycommon_database.flask_restplus_errors import ValidationFailed, ModelCouldNotBeFound

logger = logging.getLogger(__name__)


@enum.unique
class IndexType(enum.IntEnum):
    Unique = 1
    Other = 2


class Column:
    """
    Definition of a Mongo document field.
    This field is used to:
    - Validate a value.
    - Deserialize a value to a valid Mongo one.
    - Serialize a value to a valid JSON one.
    """

    def __init__(self, field_type=None, **kwargs):
        """

        :param field_type: Python field type. Default to str.

        :param choices: Restrict valid values. Only for int, float, str and Enum fields.
        Should be a list or a function (without parameters) returning a list.
        Each list item should be of field type.
        None by default, or all enum values in case of an Enum field.
        :param counter: Custom counter definition. Only for auto incremented fields.
        Should be a tuple or a function (without parameters) returning a tuple. Content should be:
         - Counter name (field name by default), string value.
         - Counter category (table name by default), string value.
        :param default_value: Default field value returned to the client if field is not set.
        Should be of field type or a function (with dictionary as parameter) returning a value of field type.
        None by default.
        :param description: Field description used in Swagger and in error messages.
        Should be a string value. Default to None.
        :param index_type: If and how this field should be indexed.
        Value should be one of IndexType enum. Default to None (not indexed).
        :param allow_none_as_filter: If None value should be kept in queries (GET/DELETE).
        Should be a boolean value. Default to False.
        :param is_primary_key: If this field value is not allowed to be modified after insert.
        Should be a boolean value. Default to False (field value can always be modified).
        :param is_nullable: If field value is optional.
        Should be a boolean value.
        Default to True if field is not a primary key.
        Default to True if field has a default value.
        Default to True if field value should auto increment.
        Otherwise default to False.
        Note that it is not allowed to force False if field has a default value or if value should auto increment.
        :param is_required: If field value must be specified in client requests.
        Should be a boolean value. Default to False.
        :param should_auto_increment: If field should be auto incremented. Only for integer fields.
        Should be a boolean value. Default to False.
        TODO Introduce min and max length, regex
        """
        self.field_type = field_type or str
        name = kwargs.pop('name', None)
        if name:
            self._update_name(name)
        self.get_choices = self._to_get_choices(kwargs.pop('choices', None))
        self.get_counter = self._to_get_counter(kwargs.pop('counter', None))
        self.get_default_value = self._to_get_default_value(kwargs.pop('default_value', None))
        self.description = kwargs.pop('description', None)
        self.index_type = kwargs.pop('index_type', None)

        self.allow_none_as_filter = bool(kwargs.pop('allow_none_as_filter', False))
        self.is_primary_key = bool(kwargs.pop('is_primary_key', False))
        self.should_auto_increment = bool(kwargs.pop('should_auto_increment', False))
        if self.should_auto_increment and self.field_type is not int:
            raise Exception('Only int fields can be auto incremented.')
        self.is_nullable = bool(kwargs.pop('is_nullable', True))
        if not self.is_nullable:
            if self.should_auto_increment:
                raise Exception('A field cannot be mandatory and auto incremented at the same time.')
            if self.get_default_value({}):
                raise Exception('A field cannot be mandatory and having a default value at the same time.')
        else:
            # Field will be optional only if it is not a primary key without default value and not auto incremented
            self.is_nullable = not self.is_primary_key or self.get_default_value({}) or self.should_auto_increment
        self.is_required = bool(kwargs.pop('is_required', False))

    def _update_name(self, name):
        if '.' in name:
            raise Exception(f'{name} is not a valid name. Dots are not allowed in Mongo field names.')
        self.name = name
        if '_id' == self.name:
            self.field_type = ObjectId

    def _to_get_counter(self, counter):
        if counter:
            return counter if callable(counter) else lambda model_as_dict: counter
        return lambda model_as_dict: (self.name,)

    def _to_get_choices(self, choices):
        """
        Return a function without arguments returning the choices.
        :param choices: A list of choices or a function providing choices (or None).
        """
        if choices:
            return choices if callable(choices) else lambda: choices
        elif isinstance(self.field_type, enum.EnumMeta):
            return lambda: list(self.field_type.__members__.keys())
        return lambda: None

    def _to_get_default_value(self, default_value):
        if default_value:
            return default_value if callable(default_value) else (lambda model_as_dict: default_value)
        return lambda model_as_dict: None

    def __str__(self):
        return f'{self.name}'

    def validate_query(self, filters: dict) -> dict:
        """
        Validate a get or delete request.

        :param filters: Provided filters.
        Each entry if composed of the field name associated to a value.
        :return: Validation errors that might have occurred. Empty if no error occurred.
        Each entry if composed of the field name associated to a list of error messages.
        """
        value = filters.get(self.name)
        if value is None:
            return {}
        return self._validate_value(value)

    def validate_insert(self, document: dict) -> dict:
        """
        Validate data on insert.
        Even if this method is the same one as validate_update, users with a custom field type might want
        to perform a different validation in case of insert and update (typically checking for missing fields)

        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        value = document.get(self.name)
        if value is None:
            if not self.is_nullable:
                return {self.name: ['Missing data for required field.']}
            return {}
        return self._validate_value(value)

    def validate_update(self, document: dict) -> dict:
        """
        Validate data on update.
        Even if this method is the same one as validate_insert, users with a custom field type might want
        to perform a different validation in case of insert and update (typically not checking for missing fields)

        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        value = document.get(self.name)
        if value is None:
            if not self.is_nullable:
                return {self.name: ['Missing data for required field.']}
            return {}
        return self._validate_value(value)

    def _validate_value(self, value) -> dict:
        """
        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        if self.field_type == datetime.datetime:
            if isinstance(value, str):
                try:
                    value = dateutil.parser.parse(value)
                except:
                    return {self.name: ['Not a valid datetime.']}
        elif self.field_type == datetime.date:
            if isinstance(value, str):
                try:
                    value = dateutil.parser.parse(value).date()
                except:
                    return {self.name: ['Not a valid date.']}
        elif isinstance(self.field_type, enum.EnumMeta):
            if isinstance(value, str):
                if value not in self.get_choices():
                    return {self.name: [f'Value "{value}" is not within {self.get_choices()}.']}
                return {}  # Consider string values valid for Enum type
        elif self.field_type == ObjectId:
            if not isinstance(value, ObjectId):
                try:
                    value = ObjectId(value)
                except BSONError as e:
                    return {self.name: [e.args[0]]}
        elif self.field_type == str:
            if isinstance(value, str):
                if self.get_choices() and value not in self.get_choices():
                    return {self.name: [f'Value "{value}" is not within {self.get_choices()}.']}
        elif self.field_type == int:
            if isinstance(value, int):
                if self.get_choices() and value not in self.get_choices():
                    return {self.name: [f'Value "{value}" is not within {self.get_choices()}.']}
        elif self.field_type == float:
            if isinstance(value, int):
                value = float(value)
            if isinstance(value, float):
                if self.get_choices() and value not in self.get_choices():
                    return {self.name: [f'Value "{value}" is not within {self.get_choices()}.']}

        if not isinstance(value, self.field_type):
            return {self.name: [f'Not a valid {self.field_type.__name__}.']}

        return {}

    def deserialize_query(self, filters: dict):
        """
        Update this field value within provided filters to a value that can be queried in Mongo.

        :param filters: Fields that should be queried.
        """
        value = filters.get(self.name)
        if value is None:
            if not self.allow_none_as_filter:
                filters.pop(self.name, None)
        else:
            filters[self.name] = self._deserialize_value(value)

    def deserialize_insert(self, document: dict):
        """
        Update this field value within the document to a value that can be inserted in Mongo.

        :param document: Document that should be inserted.
        """
        value = document.get(self.name)
        if value is None:
            # Ensure that None value are not stored to save space and allow to change default value.
            document.pop(self.name, None)
        else:
            document[self.name] = self._deserialize_value(value)

    def deserialize_update(self, document: dict):
        """
        Update this field value within the document to a value that can be inserted (updated) in Mongo.

        :param document: Updated version of an already inserted document (can be partial).
        """
        value = document.get(self.name)
        if value is None:
            # Ensure that None value are not stored to save space and allow to change default value.
            document.pop(self.name, None)
        else:
            document[self.name] = self._deserialize_value(value)

    def _deserialize_value(self, value):
        """
        Convert this field value to the proper value that can be inserted in Mongo.

        :param value: Received field value.
        :return Mongo valid value.
        """
        if value is None:
            return None

        if self.field_type == datetime.datetime:
            if isinstance(value, str):
                value = dateutil.parser.parse(value)
        elif self.field_type == datetime.date:
            if isinstance(value, str):
                value = dateutil.parser.parse(value)
            elif isinstance(value, datetime.date):
                # dates cannot be stored in Mongo, use datetime instead
                if not isinstance(value, datetime.datetime):
                    value = datetime.datetime.combine(value, datetime.datetime.min.time())
                # Ensure that time is not something else than midnight
                else:
                    value = datetime.datetime.combine(value.date(), datetime.datetime.min.time())
        elif isinstance(self.field_type, enum.EnumMeta):
            # Enum cannot be stored in Mongo, use enum value instead
            if isinstance(value, enum.Enum):
                value = value.value
            elif isinstance(value, str):
                value = self.field_type[value].value
        elif self.field_type == ObjectId:
            if not isinstance(value, ObjectId):
                value = ObjectId(value)

        return value

    def serialize(self, document: dict):
        """
        Update Mongo field value within this document to a valid JSON one.

        :param document: Document (as stored within database).
        """
        value = document.get(self.name)

        if value is None:
            document[self.name] = self.get_default_value(document)
        elif self.field_type == datetime.datetime:
            document[self.name] = value.isoformat()  # TODO Time Offset is missing to be fully compliant with RFC
        elif self.field_type == datetime.date:
            document[self.name] = value.date().isoformat()
        elif isinstance(self.field_type, enum.EnumMeta):
            document[self.name] = self.field_type(value).name
        elif self.field_type == ObjectId:
            document[self.name] = str(value)


class DictColumn(Column):
    """
    Definition of a Mongo document dictionary field.
    If you do not want to validate the content of this dictionary use a Column(dict) instead.
    """

    def __init__(self, fields: Dict[str, Column], index_fields: Dict[str, Column] = None, **kwargs):
        """
        :param fields: Definition of this dictionary.
        Should be a dictionary or a function (with dictionary as parameter) returning a dictionary.
        Keys are field names and associated values are Column.
        :param index_fields: Definition of all possible dictionary fields.
        This is used to identify every possible index fields.
        Should be a dictionary or a function (with dictionary as parameter) returning a dictionary.
        Keys are field names and associated values are Column.
        Default to fields.
        :param default_value: Default field value returned to the client if field is not set.
        Should be a dictionary or a function (with dictionary as parameter) returning a dictionary.
        None by default.
        :param description: Field description used in Swagger and in error messages.
        Should be a string value. Default to None.
        :param index_type: If and how this field should be indexed.
        Value should be one of IndexType enum. Default to None (not indexed).
        :param allow_none_as_filter: If None value should be kept in queries (GET/DELETE).
        Should be a boolean value. Default to False.
        :param is_primary_key: If this field value is not allowed to be modified after insert.
        Should be a boolean value. Default to False (field value can always be modified).
        :param is_nullable: If field value is optional.
        Should be a boolean value.
        Default to True if field is not a primary key.
        Default to True if field has a default value.
        Otherwise default to False.
        Note that it is not allowed to force False if field has a default value.
        :param is_required: If field value must be specified in client requests.
        Should be a boolean value. Default to False.
        """
        kwargs.pop('field_type', None)

        if not fields:
            raise Exception('fields is a mandatory parameter.')

        self.get_fields = fields if callable(fields) else lambda model_as_dict: fields

        if index_fields:
            self.get_index_fields = index_fields if callable(index_fields) else lambda model_as_dict: index_fields
        else:
            self.get_index_fields = self.get_fields

        if bool(kwargs.get('is_nullable', True)):  # Ensure that there will be a default value if field is nullable
            kwargs['default_value'] = lambda model_as_dict: {
                field_name: field.get_default_value(model_as_dict)
                for field_name, field in self.get_fields(model_as_dict or {}).items()
            }

        Column.__init__(self, dict, **kwargs)

    def _description_model(self, model_as_dict: dict):
        """
        :param model_as_dict: Data provided by the user or empty in case this method is called in another context.
        :return: A CRUDModel describing every dictionary fields.
        """

        class FakeModel(CRUDModel):
            pass

        for name, column in self.get_fields(model_as_dict or {}).items():
            column._update_name(name)
            FakeModel.__fields__.append(column)

        return FakeModel

    def _index_description_model(self, model_as_dict: dict):
        """
        :param model_as_dict: Data provided by the user or empty in case this method is called in another context.
        :return: A CRUDModel describing every index fields.
        """

        class FakeModel(CRUDModel):
            pass

        for name, column in self.get_index_fields(model_as_dict or {}).items():
            column._update_name(name)
            FakeModel.__fields__.append(column)

        return FakeModel

    def _get_index_fields(self, index_type: IndexType, model_as_dict: dict, prefix: str) -> List[str]:
        return self._index_description_model(model_as_dict)._get_index_fields(index_type, model_as_dict,
                                                                              f'{prefix}{self.name}.')

    def validate_insert(self, model_as_dict: dict) -> dict:
        errors = Column.validate_insert(self, model_as_dict)
        if not errors:
            value = model_as_dict.get(self.name)
            if value is not None:
                try:
                    description_model = self._description_model(model_as_dict)
                    errors.update({
                        f'{self.name}.{field_name}': field_errors
                        for field_name, field_errors in description_model.validate_insert(value).items()
                    })
                except Exception as e:
                    errors[self.name] = [str(e)]
        return errors

    def deserialize_insert(self, document: dict):
        value = document.get(self.name)
        if value is None:
            # Ensure that None value are not stored to save space and allow to change default value.
            document.pop(self.name, None)
        else:
            self._description_model(document).deserialize_insert(value)

    def validate_update(self, document: dict) -> dict:
        errors = Column.validate_update(self, document)
        if not errors:
            value = document.get(self.name)
            if value is not None:
                try:
                    description_model = self._description_model(document)
                    errors.update({
                        f'{self.name}.{field_name}': field_errors
                        for field_name, field_errors in description_model.validate_update(value).items()
                    })
                except Exception as e:
                    errors[self.name] = [str(e)]
        return errors

    def deserialize_update(self, document: dict):
        value = document.get(self.name)
        if value is None:
            # Ensure that None value are not stored to save space and allow to change default value.
            document.pop(self.name, None)
        else:
            self._description_model(document).deserialize_update(value)

    def validate_query(self, filters: dict) -> dict:
        errors = Column.validate_query(self, filters)
        if not errors:
            value = filters.get(self.name)
            if value is not None:
                try:
                    description_model = self._description_model(filters)
                    errors.update({
                        f'{self.name}.{field_name}': field_errors
                        for field_name, field_errors in description_model.validate_query(value).items()
                    })
                except Exception as e:
                    errors[self.name] = [str(e)]
        return errors

    def deserialize_query(self, filters: dict):
        value = filters.get(self.name)
        if value is None:
            if not self.allow_none_as_filter:
                filters.pop(self.name, None)
        else:
            self._description_model(filters).deserialize_query(value)

    def serialize(self, document: dict):
        value = document.get(self.name)
        if value is None:
            document[self.name] = self.get_default_value(document)
        else:
            self._description_model(document).serialize(value)


class ListColumn(Column):
    """
    Definition of a Mongo document list field.
    This list should only contains items of a specified type.
    If you do not want to validate the content of this list use a Column(list) instead.
    """

    def __init__(self, list_item_type: Column, **kwargs):
        """
        :param list_item_type: Column describing an element of this list.

        :param default_value: Default field value returned to the client if field is not set.
        Should be a dictionary or a function (with dictionary as parameter) returning a dictionary.
        None by default.
        :param description: Field description used in Swagger and in error messages.
        Should be a string value. Default to None.
        :param index_type: If and how this field should be indexed.
        Value should be one of IndexType enum. Default to None (not indexed).
        :param allow_none_as_filter: If None value should be kept in queries (GET/DELETE).
        Should be a boolean value. Default to False.
        :param is_primary_key: If this field value is not allowed to be modified after insert.
        Should be a boolean value. Default to False (field value can always be modified).
        :param is_nullable: If field value is optional.
        Should be a boolean value.
        Default to True if field is not a primary key.
        Default to True if field has a default value.
        Otherwise default to False.
        Note that it is not allowed to force False if field has a default value.
        :param is_required: If field value must be specified in client requests.
        Should be a boolean value. Default to False.        """
        kwargs.pop('field_type', None)
        self.list_item_column = list_item_type
        Column.__init__(self, list, **kwargs)

    def _update_name(self, name):
        Column._update_name(self, name)
        self.list_item_column._update_name(name)

    def validate_insert(self, document: dict) -> dict:
        errors = Column.validate_insert(self, document)
        if not errors:
            values = document.get(self.name) or []
            for index, value in enumerate(values):
                document_with_list_item = {**document, self.name: value}
                errors.update({
                    f'{field_name}[{index}]': field_errors
                    for field_name, field_errors in self.list_item_column.validate_insert(document_with_list_item).items()
                })
        return errors

    def deserialize_insert(self, document: dict):
        values = document.get(self.name)
        if values is None:
            # Ensure that None value are not stored to save space and allow to change default value.
            document.pop(self.name, None)
        else:
            new_values = []
            for value in values:
                document_with_list_item = {**document, self.name: value}
                self.list_item_column.deserialize_insert(document_with_list_item)
                if self.name in document_with_list_item:
                    new_values.append(document_with_list_item[self.name])

            document[self.name] = new_values

    def validate_update(self, document: dict) -> dict:
        errors = Column.validate_update(self, document)
        if not errors:
            values = document[self.name]
            for index, value in enumerate(values):
                document_with_list_item = {**document, self.name: value}
                errors.update({
                    f'{field_name}[{index}]': field_errors
                    for field_name, field_errors in self.list_item_column.validate_update(document_with_list_item).items()
                })
        return errors

    def deserialize_update(self, document: dict):
        values = document.get(self.name)
        if values is None:
            # Ensure that None value are not stored to save space and allow to change default value.
            document.pop(self.name, None)
        else:
            new_values = []
            for value in values:
                document_with_list_item = {**document, self.name: value}
                self.list_item_column.deserialize_update(document_with_list_item)
                if self.name in document_with_list_item:
                    new_values.append(document_with_list_item[self.name])

            document[self.name] = new_values

    def validate_query(self, filters: dict) -> dict:
        errors = Column.validate_query(self, filters)
        if not errors:
            values = filters.get(self.name) or []
            for index, value in enumerate(values):
                filters_with_list_item = {**filters, self.name: value}
                errors.update({
                    f'{field_name}[{index}]': field_errors
                    for field_name, field_errors in self.list_item_column.validate_query(filters_with_list_item).items()
                })
        return errors

    def deserialize_query(self, filters: dict):
        values = filters.get(self.name)
        if values is None:
            if not self.allow_none_as_filter:
                filters.pop(self.name, None)
        else:
            new_values = []
            for value in values:
                filters_with_list_item = {**filters, self.name: value}
                self.list_item_column.deserialize_query(filters_with_list_item)
                if self.name in filters_with_list_item:
                    new_values.append(filters_with_list_item[self.name])

            filters[self.name] = new_values

    def serialize(self, document: dict):
        values = document.get(self.name)
        if values is None:
            document[self.name] = self.get_default_value(document)
        else:
            # TODO use list comprehension
            new_values = []
            for value in values:
                document_with_list_item = {**document, self.name: value}
                self.list_item_column.serialize(document_with_list_item)
                new_values.append(document_with_list_item[self.name])

            document[self.name] = new_values


def to_mongo_field(attribute):
    attribute[1]._update_name(attribute[0])
    return attribute[1]


class CRUDModel:
    """
    Class providing CRUD helper methods for a Mongo model.
    __collection__ class property must be specified in Model.
    __counters__ class property must be specified in Model.
    Calling load_from(...) will provide you those properties.
    """
    __tablename__ = None  # Name of the collection described by this model
    __collection__ = None  # Mongo collection
    __counters__ = None  # Mongo counters collection (to increment fields)
    __fields__: List[Column] = []  # All Mongo fields within this model
    audit_model = None

    def __init_subclass__(cls, base=None, table_name: str=None, audit: bool=False, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.__tablename__ = table_name
        cls.__fields__ = [to_mongo_field(attribute) for attribute in inspect.getmembers(cls) if
                          isinstance(attribute[1], Column)]
        if base is not None:  # Allow to not provide base to create fake models
            cls.__collection__ = base[cls.__tablename__]
            cls.__counters__ = base['counters']
            cls.update_indexes({})
        if audit:
            from pycommon_database.audit_mongo import _create_from
            cls.audit_model = _create_from(cls, base)
        else:
            cls.audit_model = None  # Ensure no circular reference when creating the audit

    @classmethod
    def update_indexes(cls, document: dict):
        """
        Drop all indexes and recreate them.
        As advised in https://docs.mongodb.com/manual/tutorial/manage-indexes/#modify-an-index
        """
        logger.info('Updating indexes...')
        cls.__collection__.drop_indexes()
        cls._create_indexes(IndexType.Unique, document)
        cls._create_indexes(IndexType.Other, document)
        logger.info('Indexes updated.')
        if cls.audit_model:
            cls.audit_model.update_indexes(document)

    @classmethod
    def _create_indexes(cls, index_type: IndexType, document: dict):
        """
        Create indexes of specified type.
        :param document: Data specified by the user at the time of the index creation.
        """
        try:
            criteria = [
                (field_name, pymongo.ASCENDING)
                for field_name in cls._get_index_fields(index_type, document, '')
            ]
            if criteria:
                # Avoid using auto generated index name that might be too long
                index_name = f'uidx{cls.__collection__.name}' if index_type == IndexType.Unique else f'idx{cls.__collection__.name}'
                logger.info(
                    f"Create {index_name} {index_type.name} index on {cls.__collection__.name} using {criteria} criteria.")
                cls.__collection__.create_index(criteria, unique=index_type == IndexType.Unique, name=index_name)
        except pymongo.errors.DuplicateKeyError:
            logger.exception(f'Duplicate key found for {criteria} criteria when creating a {index_type.name} index.')
            raise

    @classmethod
    def _get_index_fields(cls, index_type: IndexType, document: dict, prefix: str) -> List[str]:
        """
        In case a field is a dictionary and some fields within it should be indexed, override this method.
        """
        index_fields = [f'{prefix}{field.name}' for field in cls.__fields__ if field.index_type == index_type]
        for field in cls.__fields__:
            if isinstance(field, DictColumn):
                index_fields.extend(field._get_index_fields(index_type, document, prefix))
        return index_fields

    @classmethod
    def get(cls, **filters) -> dict:
        """
        Return the document matching provided filters.
        """
        errors = cls.validate_query(filters)
        if errors:
            raise ValidationFailed(filters, errors)

        cls.deserialize_query(filters)

        if cls.__collection__.count(filters) > 1:
            raise ValidationFailed(filters, message='More than one result: Consider another filtering.')

        document = cls.__collection__.find_one(filters)
        return cls.serialize(document)

    @classmethod
    def get_all(cls, **filters) -> List[dict]:
        """
        Return all documents matching provided filters.
        """
        limit = filters.pop('limit', 0) or 0
        offset = filters.pop('offset', 0) or 0
        errors = cls.validate_query(filters)
        if errors:
            raise ValidationFailed(filters, errors)

        cls.deserialize_query(filters)

        documents = cls.__collection__.find(filters, skip=offset, limit=limit)
        return [cls.serialize(document) for document in documents]

    @classmethod
    def get_history(cls, **filters) -> List[dict]:
        """
        Return all documents matching filters.
        """
        return cls.get_all(**filters)

    @classmethod
    def rollback_to(cls, **filters) -> int:
        """
        All records matching the query and valid at specified validity will be considered as valid.
        :return Number of records updated.
        """
        return 0

    @classmethod
    def validate_query(cls, filters: dict) -> dict:
        """
        Validate data queried.
        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        queried_fields_names = [field.name for field in cls.__fields__ if field.name in filters]
        unknown_fields = [field_name for field_name in filters if field_name not in queried_fields_names]
        filters_without_dot_notation = copy.deepcopy(filters)
        for unknown_field in unknown_fields:
            # Convert dot notation fields into known fields to be able to validate them
            known_field, field_value = cls._handle_dot_notation(unknown_field, filters_without_dot_notation[unknown_field])
            if known_field:
                previous_dict = filters_without_dot_notation.setdefault(known_field.name, {})
                previous_dict.update(field_value)

        errors = {}

        for field in [field for field in cls.__fields__ if field.name in filters_without_dot_notation]:
            errors.update(field.validate_query(filters_without_dot_notation))

        return errors

    @classmethod
    def deserialize_query(cls, filters: dict):
        queried_fields_names = [field.name for field in cls.__fields__ if field.name in filters]
        unknown_fields = [field_name for field_name in filters if field_name not in queried_fields_names]
        dot_model_as_dict = {}

        for unknown_field in unknown_fields:
            known_field, field_value = cls._handle_dot_notation(unknown_field, filters[unknown_field])
            del filters[unknown_field]
            if known_field:
                previous_dict = dot_model_as_dict.setdefault(known_field.name, {})
                previous_dict.update(field_value)
            else:
                logger.warning(f'Skipping unknown field {unknown_field}.')

        # Deserialize dot notation values
        for field in [field for field in cls.__fields__ if field.name in dot_model_as_dict]:
            field.deserialize_query(dot_model_as_dict)
            # Put back deserialized values as dot notation fields
            for inner_field_name, value in dot_model_as_dict[field.name].items():
                filters[f'{field.name}.{inner_field_name}'] = value

        for field in [field for field in cls.__fields__ if field.name in filters]:
            field.deserialize_query(filters)

    @classmethod
    def _handle_dot_notation(cls, field_name: str, value) -> (Column, dict):
        parent_field = field_name.split('.', maxsplit=1)
        if len(parent_field) == 2:
            for field in cls.__fields__:
                if field.name == parent_field[0] and field.field_type == dict:
                    return field, {parent_field[1]: value}
        return None, None

    @classmethod
    def serialize(cls, document: dict) -> dict:
        if not document:
            return {}

        for field in cls.__fields__:
            field.serialize(document)

        # Make sure fields that were stored in a previous version of a model are not returned if removed since then
        # It also ensure _id can be skipped unless specified otherwise in the model
        known_fields = [field.name for field in cls.__fields__]
        removed_fields = [field_name for field_name in document if field_name not in known_fields]
        if removed_fields:
            for removed_field in removed_fields:
                del document[removed_field]
            # Do not log the fact that _id is removed as it is a Mongo specific field
            removed_fields.remove('_id')
            if removed_fields:
                logger.debug(f'Skipping removed fields {removed_fields}.')

        return document

    @classmethod
    def add(cls, document: dict) -> dict:
        """
        Add a model formatted as a dictionary.

        :raises ValidationFailed in case validation fail.
        :returns The inserted model formatted as a dictionary.
        """
        errors = cls.validate_insert(document)
        if errors:
            raise ValidationFailed(document, errors)

        cls.deserialize_insert(document)
        try:
            cls._insert_one(document)
            return cls.serialize(document)
        except pymongo.errors.DuplicateKeyError:
            raise ValidationFailed(cls.serialize(document), message='This document already exists.')

    @classmethod
    def add_all(cls, documents: List[dict]) -> List[dict]:
        """
        Add models formatted as a list of dictionaries.

        :raises ValidationFailed in case validation fail.
        :returns The inserted models formatted as a list of dictionaries.
        """
        if not documents:
            raise ValidationFailed([], message='No data provided.')

        new_documents = copy.deepcopy(documents)

        errors = {}

        for index, document in enumerate(new_documents):
            document_errors = cls.validate_insert(document)
            if document_errors:
                errors[index] = document_errors
                continue

            cls.deserialize_insert(document)

        if errors:
            raise ValidationFailed(documents, errors)

        try:
            cls._insert_many(new_documents)
            return [cls.serialize(document) for document in new_documents]
        except pymongo.errors.BulkWriteError as e:
            raise ValidationFailed(documents, message=str(e.details))

    @classmethod
    def validate_insert(cls, document: dict) -> dict:
        """
        Validate data on insert.

        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        if not document:
            return {'': ['No data provided.']}

        new_document = copy.deepcopy(document)

        field_names = [field.name for field in cls.__fields__]
        unknown_fields = [field_name for field_name in new_document if field_name not in field_names]
        for unknown_field in unknown_fields:
            known_field, field_value = cls._handle_dot_notation(unknown_field, new_document[unknown_field])
            if known_field:
                previous_dict = new_document.setdefault(known_field.name, {})
                previous_dict.update(field_value)

        errors = {}

        for field in cls.__fields__:
            errors.update(field.validate_insert(new_document))

        return errors

    @classmethod
    def _remove_dot_notation(cls, document: dict):
        """
        Update document so that it does not contains dot notation fields.
        """
        field_names = [field.name for field in cls.__fields__]
        unknown_fields = [field_name for field_name in document if field_name not in field_names]
        for unknown_field in unknown_fields:
            known_field, field_value = cls._handle_dot_notation(unknown_field, document[unknown_field])
            del document[unknown_field]
            if known_field:
                previous_dict = document.setdefault(known_field.name, {})
                previous_dict.update(field_value)
            else:
                logger.warning(f'Skipping unknown field {unknown_field}.')

    @classmethod
    def deserialize_insert(cls, document: dict):
        """
        Update this model dictionary by ensuring that it contains only valid Mongo values.
        """
        cls._remove_dot_notation(document)

        for field in cls.__fields__:
            field.deserialize_insert(document)
            if field.should_auto_increment:
                document[field.name] = cls._increment(*field.get_counter(document))

    @classmethod
    def _increment(cls, counter_name: str, counter_category: str=None):
        """
        Increment a counter by one.

        :param counter_name: Name of the counter to increment. Will be created at 0 if not existing yet.
        :param counter_category: Category storing those counters. Default to model table name.
        :return: New counter value.
        """
        counter_key = {'_id': counter_category if counter_category else cls.__collection__.name}
        counter_update = {'$inc': {f'{counter_name}.counter': 1},
                          '$set': {f'{counter_name}.last_update_time': datetime.datetime.utcnow()}}
        counter_element = cls.__counters__.find_one_and_update(counter_key, counter_update,
                                                               return_document=pymongo.ReturnDocument.AFTER,
                                                               upsert=True)
        return counter_element[counter_name]['counter']

    @classmethod
    def update(cls, document: dict) -> (dict, dict):
        """
        Update a model formatted as a dictionary.

        :raises ValidationFailed in case validation fail.
        :returns A tuple containing previous document (first item) and new document (second item).
        """
        errors = cls.validate_update(document)
        if errors:
            raise ValidationFailed(document, errors)

        cls.deserialize_update(document)

        try:
            previous_document, new_document = cls._update_one(document)
            return cls.serialize(previous_document), cls.serialize(new_document)
        except pymongo.errors.DuplicateKeyError:
            raise ValidationFailed(cls.serialize(document), message='This document already exists.')

    @classmethod
    def validate_update(cls, document: dict) -> dict:
        """
        Validate data on update.

        :return: Validation errors that might have occurred. Empty if no error occurred.
        """
        if not document:
            return {'': ['No data provided.']}

        new_document = copy.deepcopy(document)

        updated_field_names = [field.name for field in cls.__fields__ if field.name in new_document]
        unknown_fields = [field_name for field_name in new_document if field_name not in updated_field_names]
        for unknown_field in unknown_fields:
            # Convert dot notation fields into known fields to be able to validate them
            known_field, field_value = cls._handle_dot_notation(unknown_field, new_document[unknown_field])
            if known_field:
                previous_dict = new_document.setdefault(known_field.name, {})
                previous_dict.update(field_value)

        errors = {}

        # Also ensure that primary keys will contain a valid value
        updated_fields = [field for field in cls.__fields__ if field.name in new_document or field.is_primary_key]
        for field in updated_fields:
            errors.update(field.validate_update(new_document))

        return errors

    @classmethod
    def deserialize_update(cls, document: dict):
        """
        Update this model dictionary by ensuring that it contains only valid Mongo values.
        """
        updated_field_names = [field.name for field in cls.__fields__ if field.name in document]
        unknown_fields = [field_name for field_name in document if field_name not in updated_field_names]
        dot_model_as_dict = {}

        for unknown_field in unknown_fields:
            known_field, field_value = cls._handle_dot_notation(unknown_field, document[unknown_field])
            del document[unknown_field]
            if known_field:
                previous_dict = dot_model_as_dict.setdefault(known_field.name, {})
                previous_dict.update(field_value)
            else:
                logger.warning(f'Skipping unknown field {unknown_field}.')

        document_without_dot_notation = {**document, **dot_model_as_dict}
        # Deserialize dot notation values
        for field in [field for field in cls.__fields__ if field.name in dot_model_as_dict]:
            # Ensure that every provided field will be provided as deserialization might rely on another field
            field.deserialize_update(document_without_dot_notation)
            # Put back deserialized values as dot notation fields
            for inner_field_name, value in document_without_dot_notation[field.name].items():
                document[f'{field.name}.{inner_field_name}'] = value

        updated_fields = [field for field in cls.__fields__ if field.name in document or field.is_primary_key]
        for field in updated_fields:
            field.deserialize_update(document)

    @classmethod
    def remove(cls, **filters) -> int:
        """
        Remove the model(s) matching those criterion.

        :returns Number of removed documents.
        """
        errors = cls.validate_query(filters)
        if errors:
            raise ValidationFailed(filters, errors)

        cls.deserialize_query(filters)

        return cls._delete_many(filters)

    @classmethod
    def _insert_many(cls, documents: List[dict]):
        cls.__collection__.insert_many(documents)
        if cls.audit_model:
            for document in documents:
                cls.audit_model.audit_add(document)

    @classmethod
    def _insert_one(cls, document: dict) -> dict:
        cls.__collection__.insert_one(document)
        if cls.audit_model:
            cls.audit_model.audit_add(document)
        return document

    @classmethod
    def _update_one(cls, document: dict) -> (dict, dict):
        document_keys = cls._to_primary_keys_model(document)
        previous_document = cls.__collection__.find_one(document_keys)
        if not previous_document:
            raise ModelCouldNotBeFound(document_keys)

        new_document = cls.__collection__.find_one_and_update(document_keys, {'$set': document},
                                                              return_document=pymongo.ReturnDocument.AFTER)
        if cls.audit_model:
            cls.audit_model.audit_update(new_document)
        return previous_document, new_document

    @classmethod
    def _delete_many(cls, filters: dict) -> int:
        if cls.audit_model:
            cls.audit_model.audit_remove(**filters)
        return cls.__collection__.delete_many(filters).deleted_count

    @classmethod
    def _to_primary_keys_model(cls, document: dict) -> dict:
        # TODO Compute primary key field names only once
        primary_key_field_names = [field.name for field in cls.__fields__ if field.is_primary_key]
        return {field_name: value for field_name, value in document.items() if
                field_name in primary_key_field_names}

    @classmethod
    def query_get_parser(cls):
        query_get_parser = cls._query_parser()
        query_get_parser.add_argument('limit', type=inputs.positive)
        query_get_parser.add_argument('offset', type=inputs.natural)
        return query_get_parser

    @classmethod
    def query_get_history_parser(cls):
        query_get_parser = cls._query_parser()
        query_get_parser.add_argument('limit', type=inputs.positive)
        query_get_parser.add_argument('offset', type=inputs.natural)
        return query_get_parser

    @classmethod
    def query_delete_parser(cls):
        return cls._query_parser()

    @classmethod
    def query_rollback_parser(cls):
        return  # Only VersionedCRUDModel allows rollback

    @classmethod
    def _query_parser(cls):
        query_parser = reqparse.RequestParser()
        for field in cls.__fields__:
            cls._add_field_to_query_parser(query_parser, field)
        return query_parser

    @classmethod
    def _add_field_to_query_parser(cls, query_parser, field: Column, prefix=''):
        if isinstance(field, DictColumn):
            # Describe every dict column field as dot notation
            for inner_field in field._description_model({}).__fields__:
                cls._add_field_to_query_parser(query_parser, inner_field, f'{field.name}.')
        elif isinstance(field, ListColumn):
            # Note that List of dict or list of list might be wrongly parsed
            query_parser.add_argument(
                f'{prefix}{field.name}',
                required=field.is_required,
                type=_get_python_type(field.list_item_column),
                action='append',
                store_missing=not field.allow_none_as_filter
            )
        elif field.field_type == list:
            query_parser.add_argument(
                f'{prefix}{field.name}',
                required=field.is_required,
                type=str,  # Consider anything as valid, thus consider as str in query
                action='append',
                store_missing=not field.allow_none_as_filter
            )
        else:
            query_parser.add_argument(
                f'{prefix}{field.name}',
                required=field.is_required,
                type=_get_python_type(field),
                store_missing=not field.allow_none_as_filter
            )

    @classmethod
    def description_dictionary(cls) -> dict:
        description = {
            'collection': cls.__tablename__,
        }
        for field in cls.__fields__:
            description[field.name] = field.name
        return description

    @classmethod
    def json_post_model(cls, namespace):
        return cls._model_with_all_fields(namespace)

    @classmethod
    def json_put_model(cls, namespace):
        return cls._model_with_all_fields(namespace)

    @classmethod
    def get_response_model(cls, namespace):
        return cls._model_with_all_fields(namespace)

    @classmethod
    def get_history_response_model(cls, namespace):
        return cls._model_with_all_fields(namespace)

    @classmethod
    def _model_with_all_fields(cls, namespace):
        return namespace.model(cls.__name__, cls._flask_restplus_fields(namespace))

    @classmethod
    def _flask_restplus_fields(cls, namespace) -> dict:
        return {field.name: cls._to_flask_restplus_field(namespace, field) for field in cls.__fields__}

    @classmethod
    def _to_flask_restplus_field(cls, namespace, field: Column):
        if isinstance(field, DictColumn):
            dict_fields = field._description_model({})._flask_restplus_fields(namespace)
            if dict_fields:
                dict_model = namespace.model('_'.join(dict_fields), dict_fields)
                # Nested field cannot contains nothing
                return flask_restplus_fields.Nested(
                    dict_model,
                    required=field.is_required,
                    example=_get_example(field),
                    description=field.description,
                    enum=field.get_choices(),
                    default=field.get_default_value({}),
                    readonly=field.should_auto_increment,
                )
            else:
                return _get_flask_restplus_type(field)(
                    required=field.is_required,
                    example=_get_example(field),
                    description=field.description,
                    enum=field.get_choices(),
                    default=field.get_default_value({}),
                    readonly=field.should_auto_increment,
                )
        elif isinstance(field, ListColumn):
            return flask_restplus_fields.List(
                cls._to_flask_restplus_field(namespace, field.list_item_column),
                required=field.is_required,
                example=_get_example(field),
                description=field.description,
                enum=field.get_choices(),
                default=field.get_default_value({}),
                readonly=field.should_auto_increment,
            )
        elif field.field_type == list:
            return flask_restplus_fields.List(
                flask_restplus_fields.String,
                required=field.is_required,
                example=_get_example(field),
                description=field.description,
                enum=field.get_choices(),
                default=field.get_default_value({}),
                readonly=field.should_auto_increment,
            )
        else:
            return _get_flask_restplus_type(field)(
                required=field.is_required,
                example=_get_example(field),
                description=field.description,
                enum=field.get_choices(),
                default=field.get_default_value({}),
                readonly=field.should_auto_increment,
            )

    @classmethod
    def flask_restplus_description_fields(cls) -> dict:
        exported_fields = {
            'collection': flask_restplus_fields.String(required=True, example='collection',
                                                       description='Collection name'),
        }

        exported_fields.update({
            field.name: flask_restplus_fields.String(
                required=field.is_required,
                example='column',
                description=field.description,
            )
            for field in cls.__fields__
        })
        return exported_fields


def _load(database_connection_url: str, create_models_func: callable):
    """
    Create all necessary tables and perform the link between models and underlying database connection.

    :param database_connection_url: URL formatted as a standard database connection string (Mandatory).
    :param create_models_func: Function that will be called to create models and return them (instances of CRUDModel)
    (Mandatory).
    """
    logger.info(f'Connecting to {database_connection_url}...')
    database_name = os.path.basename(database_connection_url)
    if database_connection_url.startswith('mongomock'):
        import mongomock  # This is a test dependency only
        client = mongomock.MongoClient()
    else:
        client = pymongo.MongoClient(database_connection_url)
    base = client[database_name]
    logger.debug(f'Creating models...')
    create_models_func(base)
    return base


def _reset(base):
    """
    If the database was already created, then drop all tables and recreate them all.
    """
    if base:
        for collection in base._collections.values():
            _reset_collection(base, collection)


def _reset_collection(base, collection):
    """
    Reset collection and keep indexes.
    """
    logger.info(f'Resetting all data related to "{collection.name}" collection...')
    nb_removed = collection.delete_many({}).deleted_count
    logger.info(f'{nb_removed} records deleted.')

    logger.info(f'Resetting counters."{collection.name}".')
    nb_removed = base['counters'].delete_many({'_id': collection.name}).deleted_count
    logger.info(f'{nb_removed} counter records deleted')


def _get_flask_restplus_type(field: Column):
    """
    Return the Flask RestPlus field type (as a class) corresponding to this Mongo field.
    Default to String.
    """
    if field.field_type == int:
        return flask_restplus_fields.Integer
    if field.field_type == float:
        return flask_restplus_fields.Float
    if field.field_type == bool:
        return flask_restplus_fields.Boolean
    if field.field_type == datetime.date:
        return flask_restplus_fields.Date
    if field.field_type == datetime.datetime:
        return flask_restplus_fields.DateTime
    if field.field_type == list:
        return flask_restplus_fields.List
    if field.field_type == dict:
        return flask_restplus_fields.Raw

    return flask_restplus_fields.String


def _get_example(field: Column):
    if isinstance(field, DictColumn):
        return (
            {
                field_name: _get_example(dict_field)
                for field_name, dict_field in field.get_fields({}).items()
            }
        )

    if isinstance(field, ListColumn):
        return [_get_example(field.list_item_column)]

    if field.get_default_value({}) is not None:
        return field.get_default_value({})

    return field.get_choices()[0] if field.get_choices() else _get_default_example(field)


def _get_default_example(field: Column):
    """
    Return an Example value corresponding to this Mongodb field.
    """
    if field.field_type == int:
        return 1
    if field.field_type == float:
        return 1.4
    if field.field_type == bool:
        return True
    if field.field_type == datetime.date:
        return '2017-09-24'
    if field.field_type == datetime.datetime:
        return '2017-09-24T15:36:09'
    if field.field_type == list:
        return [
            f'1st {field.name} sample',
            f'2nd {field.name} sample',
        ]
    if field.field_type == dict:
        return {
            f'1st {field.name} key': f'1st {field.name} sample',
            f'2nd {field.name} key': f'2nd {field.name} sample',
        }
    if field.field_type == ObjectId:
        return '1234567890QBCDEF01234567'
    return f'sample {field.name}'


def _get_python_type(field: Column):
    """
    Return the Python type corresponding to this Mongo field.

    :raises Exception if field type is not managed yet.
    """
    if field.field_type == bool:
        return inputs.boolean
    if field.field_type == datetime.date:
        return inputs.date_from_iso8601
    if field.field_type == datetime.datetime:
        return inputs.datetime_from_iso8601
    if isinstance(field.field_type, enum.EnumMeta):
        return str
    if field.field_type == dict:
        return json.loads
    if field.field_type == list:
        return json.loads
    if field.field_type == ObjectId:
        return str

    return field.field_type
