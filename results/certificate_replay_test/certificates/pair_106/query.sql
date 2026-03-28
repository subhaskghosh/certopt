SELECT
  person.firstname,
  person.lastname,
  address.city,
  address.state
FROM PERSON
LEFT JOIN ADDRESS
  ON personid = address.personid